LIGHT_CSS = """
<style>
    /* ── Light Theme ──────────────────────────────────────────────────────── */
    .stApp {
        background-color: #f8f9fa;
    }
    .stSidebar {
        background-color: #ffffff !important;
        border-right: 1px solid #e0e4ea;
    }
    h1, h2, h3, h4 {
        color: #1f4e79;
    }
    /* Primary buttons */
    .stButton > button[kind="primary"],
    button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, #1f4e79 0%, #2e86ab 100%);
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="primary"]:hover,
    button[data-testid="baseButton-primary"]:hover {
        background: linear-gradient(135deg, #163a5f 0%, #1f6e94 100%);
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(31,78,121,0.3);
    }
    /* Secondary buttons */
    .stButton > button:not([kind="primary"]) {
        border-radius: 8px;
        border: 1px solid #c8d0da;
    }
    /* Inputs */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea {
        border-radius: 8px;
        border: 1px solid #c8d0da;
        background-color: #ffffff;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #1f4e79;
        box-shadow: 0 0 0 2px rgba(31,78,121,0.12);
    }
    /* Metric cards */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border-radius: 12px;
        padding: 1rem 1.25rem;
        box-shadow: 0 2px 8px rgba(0,0,0,0.07);
        border: 1px solid #e0e4ea;
    }
    div[data-testid="stMetricValue"] {
        color: #1f4e79 !important;
        font-weight: 700;
    }
    /* DataFrame */
    .stDataFrame {
        border-radius: 8px;
        overflow: hidden;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    }
    /* Tabs */
    .stTabs [data-baseweb="tab"] {
        font-weight: 600;
        color: #666;
    }
    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        color: #1f4e79;
        border-bottom-color: #1f4e79;
    }
    /* Alerts */
    .stAlert { border-radius: 8px; }
    /* Download buttons */
    .stDownloadButton > button {
        border-radius: 8px;
        border: 1px solid #c8d0da;
    }
    /* File uploader */
    .stFileUploader {
        border-radius: 8px;
    }
    /* Progress bar */
    .stProgress > div > div {
        background-color: #1f4e79;
    }
    /* Selectbox */
    .stSelectbox > div > div {
        border-radius: 8px;
        border: 1px solid #c8d0da;
    }
    /* Toggle */
    .stToggle {
        accent-color: #1f4e79;
    }
</style>
"""

DARK_CSS = """
<style>
    /* ── Dark Theme ───────────────────────────────────────────────────────── */
    .stApp {
        background-color: #0d1117 !important;
        color: #c9d1d9 !important;
    }
    .stSidebar {
        background-color: #161b22 !important;
        border-right: 1px solid #30363d !important;
    }
    .stSidebar * {
        color: #c9d1d9 !important;
    }
    h1, h2, h3, h4 {
        color: #58a6ff !important;
    }
    p, span, label, .stMarkdown {
        color: #c9d1d9 !important;
    }
    /* Primary buttons */
    .stButton > button[kind="primary"],
    button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, #1f6feb 0%, #388bfd 100%) !important;
        color: white !important;
        border: none !important;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(135deg, #1158c7 0%, #2a7ae8 100%) !important;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(31,111,235,0.4);
    }
    /* All buttons */
    .stButton > button {
        background-color: #21262d !important;
        color: #c9d1d9 !important;
        border: 1px solid #30363d !important;
        border-radius: 8px;
    }
    /* Inputs */
    .stTextInput > div > div > input {
        background-color: #161b22 !important;
        color: #c9d1d9 !important;
        border: 1px solid #30363d !important;
        border-radius: 8px;
    }
    .stTextArea > div > div > textarea {
        background-color: #161b22 !important;
        color: #c9d1d9 !important;
        border: 1px solid #30363d !important;
        border-radius: 8px;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #58a6ff !important;
        box-shadow: 0 0 0 2px rgba(88,166,255,0.15) !important;
    }
    /* Selectbox */
    .stSelectbox > div > div {
        background-color: #161b22 !important;
        color: #c9d1d9 !important;
        border: 1px solid #30363d !important;
        border-radius: 8px;
    }
    /* Metric cards */
    div[data-testid="stMetric"] {
        background-color: #161b22 !important;
        border-radius: 12px !important;
        padding: 1rem 1.25rem !important;
        border: 1px solid #30363d !important;
    }
    div[data-testid="stMetric"] * {
        color: #c9d1d9 !important;
    }
    div[data-testid="stMetricValue"] {
        color: #58a6ff !important;
        font-weight: 700 !important;
    }
    /* DataFrame */
    .stDataFrame {
        background-color: #161b22 !important;
        border-radius: 8px;
    }
    /* Tabs */
    .stTabs [data-baseweb="tab"] {
        color: #8b949e !important;
        font-weight: 600;
    }
    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        color: #58a6ff !important;
    }
    .stTabs [data-baseweb="tab-list"] {
        background-color: #161b22 !important;
        border-bottom: 1px solid #30363d !important;
    }
    /* Alerts */
    .stAlert {
        border-radius: 8px !important;
        background-color: #161b22 !important;
        border: 1px solid #30363d !important;
    }
    div[data-testid="stNotificationContentSuccess"] {
        background-color: #1a3a1a !important;
    }
    div[data-testid="stNotificationContentError"] {
        background-color: #3a1a1a !important;
    }
    div[data-testid="stNotificationContentWarning"] {
        background-color: #2d2305 !important;
    }
    div[data-testid="stNotificationContentInfo"] {
        background-color: #0d2137 !important;
    }
    /* Checkboxes & Radios */
    .stCheckbox span,
    .stRadio span {
        color: #c9d1d9 !important;
    }
    /* File uploader */
    .stFileUploader {
        background-color: #161b22 !important;
        border: 1px dashed #30363d !important;
        border-radius: 8px !important;
    }
    .stFileUploader * {
        color: #c9d1d9 !important;
    }
    /* Progress bar */
    .stProgress > div > div {
        background-color: #1f6feb !important;
    }
    /* Download button */
    .stDownloadButton > button {
        background-color: #21262d !important;
        color: #58a6ff !important;
        border: 1px solid #30363d !important;
        border-radius: 8px;
    }
    /* HR */
    hr {
        border-color: #30363d !important;
    }
    /* Form elements background */
    .stForm {
        background-color: #161b22 !important;
        border: 1px solid #30363d !important;
        border-radius: 12px;
        padding: 1.5rem;
    }
    /* Toggle */
    .stToggle {
        accent-color: #58a6ff;
    }
    /* Caption */
    .stCaption {
        color: #8b949e !important;
    }
</style>
"""


def get_theme_css(dark_mode: bool) -> str:
    return DARK_CSS if dark_mode else LIGHT_CSS
