from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent

EXCEL_PATH = ROOT_DIR / 'Copy of Construction Status Documents all States _ Construction_updates_urlModified.xlsx'
EXCEL_SHEET = 'Construction_updates_urlModifie'

TARGET_STATES = {'GJ', 'HR', 'MH', 'UP', 'TS'}
EXCEL_COLUMNS = ['project_id', 'project_name', 'rera_reg_id', 'state_code', 'url_modified']

DOCUMENTS_DIR = ROOT_DIR / 'documents'
OUTPUT_DIR = ROOT_DIR / 'output'
LOGS_DIR = ROOT_DIR / 'logs'

MANIFEST_PATH = OUTPUT_DIR / 'manifest.json'
DROPPED_PATH = OUTPUT_DIR / 'dropped.json'
DOWNLOAD_RESULTS_PATH = OUTPUT_DIR / 'download_results.json'
VALIDATION_RESULTS_PATH = OUTPUT_DIR / 'validation_results.json'

# Download settings
DOWNLOAD_MAX_WORKERS = 5
DOWNLOAD_RATE_LIMIT = 3.0  # requests per second
DOWNLOAD_MAX_RETRIES = 3
DOWNLOAD_TIMEOUT = 60  # seconds

# Validation: if avg chars per page < this, treat as scanned/image-based
TEXT_CHARS_PER_PAGE_THRESHOLD = 100

# Step 4: Extraction
EXTRACTION_MODEL = 'claude-haiku-4-5'
EXTRACTION_MAX_TOKENS = 2048
EXTRACTION_RENDER_DPI = 150        # DPI for rendering scanned/garbled pages to images
EXTRACTION_MAX_PAGES = 8           # cap pages sent per document to control cost
EXTRACTION_MIN_CHARS_PER_PAGE = 150  # below this, text extraction is too sparse → fallback to Tier 2
EXTRACTION_RESULTS_PATH = OUTPUT_DIR / 'extraction_results.json'
EXTRACTION_BATCH_ID_PATH = OUTPUT_DIR / 'extraction_batch_id.txt'
