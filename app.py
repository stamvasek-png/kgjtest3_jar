import streamlit as st
import pandas as pd
import pulp
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import io

st.set_page_config(page_title="KGJ Strategy Expert", layout="wide")

# Výchozí parametry pro každou lokalitu
_RAB_KGJ_GAS_INPUT = 1.139          # Rabasova: spotřeba plynu KGJ [MW]
_RAB_EBOILER_EL_INPUT = 0.414       # Rabasova: max. el. příkon elektrokotle [MW]
_RAB_EBOILER_EFF = 0.98             # Rabasova: účinnost elektrokotle

LOCALITY_DEFAULTS = {
    'Behounkova': {
        'k_th': 1.09, 'k_el': 1.0, 'k_eff': 0.46, 'k_serv': 12.0, 'k_min': 55,
        'b_max': 3.91, 'b_eff': 0.95, 'ek_max': 0.61, 'ek_eff': 0.98,
        'dist_ee': 33.0, 'h_cover': 99, 'fixed_heat_price': 120.0,
    },
    'Rabasova': {
        'k_th': 0.592, 'k_el': 0.45, 'k_eff': round(0.592 / _RAB_KGJ_GAS_INPUT, 4), 'k_serv': 7.0, 'k_min': 55,
        'b_max': 0.4, 'b_eff': 0.95, 'ek_max': round(_RAB_EBOILER_EL_INPUT * _RAB_EBOILER_EFF, 3), 'ek_eff': _RAB_EBOILER_EFF,
        'dist_ee': 33.0, 'h_cover': 99, 'fixed_heat_price': 120.0,
    },
}

# Inicializace stavu aplikace
if 'fwd_data' not in st.session_state: st.session_state.fwd_data = None
if 'loc_data' not in st.session_state: st.session_state.loc_data = {'Behounkova': None, 'Rabasova': None}
if 'locality' not in st.session_state: st.session_state.locality = 'Behounkova'
if 'opt_results' not in st.session_state: st.session_state.opt_results = {'Behounkova': None, 'Rabasova': None}

st.title("🚀 KGJ Strategy & Dispatch Optimizer")

# --- VÝBĚR LOKALITY ---
btn_col1, btn_col2, _ = st.columns([1, 1, 5])
with btn_col1:
    if st.button("📍 Behounkova", type="primary" if st.session_state.locality == 'Behounkova' else "secondary", use_container_width=True):
        st.session_state.locality = 'Behounkova'
        st.rerun()
with btn_col2:
    if st.button("📍 Rabasova", type="primary" if st.session_state.locality == 'Rabasova' else "secondary", use_container_width=True):
        st.session_state.locality = 'Rabasova'
        st.rerun()

loc = st.session_state.locality
defaults = LOCALITY_DEFAULTS[loc]

st.divider()

# --- 1. KROK: TRŽNÍ DATA (FWD) ---
with st.sidebar:
    st.header("1️⃣ Tržní ceny (FWD)")
    fwd_file = st.file_uploader("Nahraj FWD křivku (Excel)", type=["xlsx"])
    
    if fwd_file:
        df_fwd_raw = pd.read_excel(fwd_file)
        df_fwd_raw.columns = [str(c).strip() for c in df_fwd_raw.columns]
        date_col = df_fwd_raw.columns[0]
        df_fwd_raw[date_col] = pd.to_datetime(df_fwd_raw[date_col], dayfirst=True)
        df_fwd_raw = df_fwd_raw.rename(columns={
            date_col: 'datetime', 
            df_fwd_raw.columns[1]: 'ee_base', 
            df_fwd_raw.columns[2]: 'gas_base'
        })
        
        years = sorted(df_fwd_raw['datetime'].dt.year.unique())
        sel_year = st.selectbox("Rok pro analýzu", years)
        
        st.subheader("🛠️ Úprava cen (Shift)")
        ee_shift = st.number_input("Posun EE [EUR/MWh]", value=0.0)
        gas_shift = st.number_input("Posun Plyn [EUR/MWh]", value=0.0)
        
        df_fwd = df_fwd_raw[df_fwd_raw['datetime'].dt.year == sel_year].copy()
        df_fwd['ee_price'] = df_fwd['ee_base'] + ee_shift
        df_fwd['gas_price'] = df_fwd['gas_base'] + gas_shift
        df_fwd['mdh'] = df_fwd['datetime'].dt.strftime('%m-%d-%H')
        st.session_state.fwd_data = df_fwd

    st.divider()
    st.header("2️⃣ Aktivní technologie")
    use_kgj = st.checkbox("Kogenerace (KGJ)", value=True)
    use_boil = st.checkbox("Plynový kotel", value=True)
    use_ek = st.checkbox("Elektrokotel", value=True)
    use_ext_heat = st.checkbox("Povolit nákup tepla (Import)", value=False)

# --- 2. KROK: ZOBRAZENÍ TRŽNÍ KŘIVKY ---
if st.session_state.fwd_data is not None:
    with st.expander("📊 Náhled tržních cen", expanded=True):
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=st.session_state.fwd_data['datetime'], y=st.session_state.fwd_data['ee_price'], name="EE Cena", line=dict(color='green')), secondary_y=False)
        fig.add_trace(go.Scatter(x=st.session_state.fwd_data['datetime'], y=st.session_state.fwd_data['gas_price'], name="Plyn Cena", line=dict(color='red')), secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)

# --- 3. KROK: PARAMETRY ---
st.header(f"📍 Parametry lokality a technologií – {loc}")

def _locked_number(label, param_key, default, **kwargs):
    """Number input s tlačítkem odemčení."""
    ul_key = f"unlock_{loc}_{param_key}"
    c_val, c_lock = st.columns([6, 1])
    with c_lock:
        unlocked = st.checkbox("🔓", key=ul_key, label_visibility="collapsed")
    with c_val:
        return st.number_input(label, value=default, disabled=not unlocked, key=f"{loc}_{param_key}", **kwargs)

def _locked_slider_int(label, param_key, default, min_v, max_v):
    """Integer slider s tlačítkem odemčení."""
    ul_key = f"unlock_{loc}_{param_key}"
    c_val, c_lock = st.columns([6, 1])
    with c_lock:
        unlocked = st.checkbox("🔓", key=ul_key, label_visibility="collapsed")
    with c_val:
        return st.slider(label, min_v, max_v, default, disabled=not unlocked, key=f"{loc}_{param_key}")

col_p1, col_p2 = st.columns(2)
params = {}

with col_p1:
    if use_kgj:
        st.info("💡 Parametry KGJ")
        params['k_th'] = _locked_number("Tepelný výkon [MW]", "k_th", defaults['k_th'])
        params['k_el'] = _locked_number("Elektrický výkon [MW]", "k_el", defaults['k_el'])
        params['k_eff'] = _locked_number("Tepelná účinnost", "k_eff", defaults['k_eff'])
        params['k_serv'] = _locked_number("Servisní náklad [EUR/hod]", "k_serv", defaults['k_serv'])
        params['k_min'] = _locked_slider_int("Minimální zatížení [%]", "k_min", defaults['k_min'], 0, 100) / 100
    if use_boil:
        st.info("🔥 Plynový kotel")
        params['b_max'] = _locked_number("Max. výkon kotle [MW]", "b_max", defaults['b_max'])
        params['b_eff'] = _locked_number("Účinnost kotle", "b_eff", defaults['b_eff'])

with col_p2:
    if use_ek:
        st.info("⚡ Elektrokotel")
        params['ek_max'] = _locked_number("Max. výkon EK [MW]", "ek_max", defaults['ek_max'])
        params['ek_eff'] = _locked_number("Účinnost EK", "ek_eff", defaults['ek_eff'])
        params['dist_ee'] = _locked_number("Distribuce nákup EE [EUR/MWh]", "dist_ee", defaults['dist_ee'])
    st.info("🏠 Systém")
    params['h_cover'] = _locked_slider_int("Minimální pokrytí potřeby [%]", "h_cover", defaults['h_cover'], 0, 100) / 100
    params['fixed_heat_price'] = _locked_number("Výkupní cena tepla [EUR/MWh]", "fixed_heat_price", defaults['fixed_heat_price'])

# --- 4. KROK: DATA LOKALITY ---
st.divider()
loc_file = st.file_uploader(f"3️⃣ Nahraj potřebu tepla pro {loc} (Excel)", type=["xlsx"], key=f"loc_file_{loc}")
if loc_file:
    df_loc = pd.read_excel(loc_file)
    df_loc.columns = [str(c).strip() for c in df_loc.columns]
    date_col = df_loc.columns[0]
    df_loc[date_col] = pd.to_datetime(df_loc[date_col], dayfirst=True)
    df_loc['mdh'] = df_loc[date_col].dt.strftime('%m-%d-%H')
    
    mapping = {df_loc.columns[2]: 'demand'}
    for c in df_loc.columns:
        if 'nákup' in c.lower(): mapping[c] = 'ext_price'
        if 'cena tepla' in c.lower() or 'prodej' in c.lower(): mapping[c] = 'heat_price'
    
    df_loc = df_loc.rename(columns=mapping)
    st.session_state.loc_data[loc] = df_loc
elif st.session_state.loc_data[loc] is not None:
    st.info(f"✅ Data pro {loc} jsou načtena ({len(st.session_state.loc_data[loc])} řádků). Nahraj nový soubor pro přepsání.")

# --- 5. KROK: OPTIMALIZACE ---
if st.session_state.fwd_data is not None and st.session_state.loc_data[loc] is not None:
    if st.button("🏁 SPUSTIT KOMPLETNÍ OPTIMALIZACI"):
        df = pd.merge(st.session_state.fwd_data, st.session_state.loc_data[loc], on='mdh', how='inner')
        T = len(df)
        model = pulp.LpProblem("Dispatcher", pulp.LpMaximize)
        
        # PROMĚNNÉ
        q_kgj = pulp.LpVariable.dicts("q_KGJ", range(T), 0, params.get('k_th', 0))
        q_boil = pulp.LpVariable.dicts("q_Boil", range(T), 0, params.get('b_max', 0))
        q_ek = pulp.LpVariable.dicts("q_EK", range(T), 0, params.get('ek_max', 0))
        q_ext = pulp.LpVariable.dicts("q_Ext", range(T), 0)
        q_deficit = pulp.LpVariable.dicts("q_Deficit", range(T), 0)
        on = pulp.LpVariable.dicts("on", range(T), 0, 1, cat="Binary")

        kgj_gas_ratio = (params.get('k_th', 1) / params.get('k_eff', 1)) / params.get('k_th', 1)
        kgj_el_ratio = params.get('k_el', 0) / params.get('k_th', 1)

        profit_total = []
        for t in range(T):
            ee = df.loc[t, 'ee_price']
            gas = df.loc[t, 'gas_price']
            hp = df.loc[t, 'heat_price'] if 'heat_price' in df.columns else params['fixed_heat_price']
            dem = df.loc[t, 'demand']
            h_req = dem * params['h_cover']

            # Bilance tepla (se Slack proměnnou deficitu)
            model += q_kgj[t] + q_boil[t] + q_ek[t] + q_ext[t] + q_deficit[t] >= h_req
            
            # Omezení technologií
            if not use_kgj: model += q_kgj[t] == 0
            else:
                model += q_kgj[t] <= params['k_th'] * on[t]
                model += q_kgj[t] >= params['k_min'] * params['k_th'] * on[t]
            
            if not use_boil: model += q_boil[t] == 0
            if not use_ek: model += q_ek[t] == 0
            if not (use_ext_heat and 'ext_price' in df.columns): model += q_ext[t] == 0

            # Cashflow
            income = (hp * (h_req - q_deficit[t])) + (ee * q_kgj[t] * kgj_el_ratio)
            costs = (gas * (q_kgj[t] * kgj_gas_ratio)) + \
                    (gas * (q_boil[t] / params.get('b_eff', 0.95))) + \
                    ((ee + params.get('dist_ee', 33)) * (q_ek[t] / params.get('ek_eff', 0.98))) + \
                    (params.get('k_serv', 12) * on[t])
            
            if use_ext_heat and 'ext_price' in df.columns:
                costs += df.loc[t, 'ext_price'] * q_ext[t]
            
            # Penalizace za deficit (vysoká cena za nedodání)
            penalty = q_deficit[t] * 5000 
            profit_total.append(income - costs - penalty)

        model += pulp.lpSum(profit_total)
        model.solve(pulp.PULP_CBC_CMD(msg=0))

        t_col = 'datetime_x' if 'datetime_x' in df.columns else ('datetime' if 'datetime' in df.columns else df.columns[0])
        res = pd.DataFrame({
            'Čas': df[t_col],
            'KGJ [MW]': [q_kgj[t].value() for t in range(T)],
            'Kotel [MW]': [q_boil[t].value() for t in range(T)],
            'EK [MW]': [q_ek[t].value() for t in range(T)],
            'Nákup [MW]': [q_ext[t].value() for t in range(T)],
            'Deficit [MW]': [q_deficit[t].value() for t in range(T)],
            'Poptávka [MW]': df['demand'] * params['h_cover'],
            'Cena EE [EUR/MWh]': df['ee_price'].values,
            'Cena plynu [EUR/MWh]': df['gas_price'].values,
        })

        st.session_state.opt_results[loc] = {
            'res': res,
            'objective': pulp.value(model.objective),
        }

# --- 6. KROK: ZOBRAZENÍ A EXPORT VÝSLEDKŮ ---
if st.session_state.opt_results[loc] is not None:
    opt = st.session_state.opt_results[loc]
    res = opt['res']

    st.success(f"Optimalizace hotova. Hrubý zisk (po penalizacích): {opt['objective']:,.0f} EUR")

    # GRAF 1: DISPATCH
    fig1 = go.Figure()
    colors = {'KGJ [MW]': '#FF9900', 'Kotel [MW]': '#1f77b4', 'EK [MW]': '#2ca02c', 'Nákup [MW]': '#d62728'}
    for c in ['KGJ [MW]', 'Kotel [MW]', 'EK [MW]', 'Nákup [MW]']:
        if res[c].sum() > 0.001:
            fig1.add_trace(go.Bar(x=res['Čas'], y=res[c], name=c, marker_color=colors[c]))
    fig1.add_trace(go.Scatter(x=res['Čas'], y=res['Poptávka [MW]'], name="Požadavek", line=dict(color='black', dash='dot')))
    fig1.update_layout(barmode='stack', title="Hodinový Dispatch zdrojů tepla [MW]", hovermode="x unified")
    st.plotly_chart(fig1, use_container_width=True)

    # GRAF 2: DEFICIT (jen pokud existuje)
    if res['Deficit [MW]'].sum() > 0.1:
        st.warning("⚠️ Systém nedokáže pokrýt veškerou poptávku!")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=res['Čas'], y=res['Deficit [MW]'], fill='tozeroy', name="Nedostatek tepla", line=dict(color='black')))
        fig2.update_layout(title="Hodinový deficit tepla (Nepokryto) [MW]", yaxis_title="Výkon [MW]")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("✅ Poptávka je plně pokryta.")

    # --- EXPORT ---
    st.divider()
    st.subheader("📥 Export výsledků optimalizace")
    all_cols = [c for c in res.columns if c != 'Čas']
    selected_cols = st.multiselect(
        "Vyberte sloupce pro náhled a export:",
        all_cols,
        default=all_cols,
        key=f"export_cols_{loc}",
    )
    export_df = res[['Čas'] + selected_cols] if selected_cols else res[['Čas']]
    st.dataframe(export_df, use_container_width=True)

    exp_col1, exp_col2 = st.columns(2)
    with exp_col1:
        csv_bytes = export_df.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="⬇️ Stáhnout jako CSV",
            data=csv_bytes,
            file_name=f"optimalizace_{loc}.csv",
            mime="text/csv",
            key=f"dl_csv_{loc}",
        )
    with exp_col2:
        xlsx_buf = io.BytesIO()
        with pd.ExcelWriter(xlsx_buf, engine='openpyxl') as writer:
            export_df.to_excel(writer, index=False, sheet_name='Optimalizace')
        xlsx_buf.seek(0)
        st.download_button(
            label="⬇️ Stáhnout jako Excel",
            data=xlsx_buf,
            file_name=f"optimalizace_{loc}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_xlsx_{loc}",

        )

