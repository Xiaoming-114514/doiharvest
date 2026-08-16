"""
DoiHarvest OA Downloader - Configuration
========================================
Modify the values below before running the script.
"""

# ============================================================
# 1. Unpaywall API Configuration
# ============================================================
# Unpaywall is free. You only need to provide your email address.
# Register at: https://unpaywall.org/products/api
# Set ENABLE_UNPAYWALL = False if you don't have an email yet (will use Crossref only).
ENABLE_UNPAYWALL = True  # <-- set to False to skip Unpaywall
UNPAYWALL_EMAIL = ""  # <-- 填入你的邮箱（Unpaywall 免费，仅用于身份识别）

# ============================================================
# 2. Input / Output Paths
# ============================================================
# Input CSV file containing DOIs. Put your file in data/ or specify a full path.
# The CSV must have a column named "DOI" (case-insensitive).
# A "Title" column is optional but recommended for better filenames.
INPUT_CSV = "data/dois.csv"

# Directory to save downloaded PDFs
PAPERS_DIR = "papers"

# Directory for log files
LOGS_DIR = "logs"

# Directory for output CSVs (non-OA list, failed list, etc.)
OUTPUT_DIR = "output"

# ============================================================
# 3. Rate Limiting & Performance
# ============================================================
# Seconds to wait between API requests (Unpaywall allows 100k/day, but be nice)
API_DELAY = 3.0  # seconds between Unpaywall API calls

# Seconds to wait between PDF downloads
DOWNLOAD_DELAY = 3.0  # seconds between PDF downloads

# Maximum concurrent PDF downloads (keep low to avoid getting blocked)
MAX_WORKERS = 3

# ============================================================
# 4. Retry Configuration
# ============================================================
MAX_RETRIES = 3  # retry attempts for failed downloads
RETRY_DELAY = 5  # seconds to wait before retrying
TIMEOUT = 30  # seconds before a request times out

# ============================================================
# 5. Crossref Configuration (Optional, used as OA fallback)
# ============================================================
# Crossref API is completely free, no key needed.
# We use it as a secondary OA source for papers Unpaywall didn't find.
ENABLE_CROSSREF = True  # set to False to skip Crossref check
CROSSREF_DELAY = 3.0  # seconds between Crossref API calls

# ============================================================
# 6. File Naming
# ============================================================
# How to name downloaded PDF files:
#   "doi"    -> 10.1038_s41467-022-31305-4.pdf
#   "title"  -> Construction_of_a_synthetic_Saccharomyces.pdf
#   "doi_title" -> 10.1038_s41467-022-31305-4_Construction_of_a_synthetic.pdf
FILE_NAMING = "doi_title"

# ============================================================
# 7. Proxy Configuration (Optional)
# ============================================================
# If you need a proxy for downloading PDFs, set it here.
# Format: {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
PROXIES = None  # set to a dict if needed

# ============================================================
# 8. MinerU OCR Configuration (Phase 3)
# ============================================================
# Path to the MinerU CLI executable (inside its venv).
# 运行 python install.py --mineru 会自动填写此路径；
# 或手动填 MinerU 的 mineru.exe 完整路径。
MINERU_EXECUTABLE = ""

# MinerU backend: "pipeline" (GPU layout model) or "vlm-engine" (VLM).
# "pipeline" is faster and more stable for batch PDF OCR.
MINERU_BACKEND = "pipeline"

# Subdirectory under output_dir where MinerU will write OCR results.
# Each PDF gets its own folder: {ocr_output}/{pdf_stem}/auto/{pdf_stem}.md
MINERU_OUTPUT_SUBDIR = "ocr_output"

# ============================================================
# 9. Phase 4: DeepSeek LLM Screening Configuration
# ============================================================
# Get your API key at: https://platform.deepseek.com/
# You can also set it from the Web interface (stored in workspace_config.json).
# Web interface value takes priority if set (non-empty).
DEEPSEEK_API_KEY = ""  # <-- 在这里或 Web 界面填入你的 DeepSeek API Key
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-pro"

# Maximum characters of MD content to send per paper.
# 90000 已足够覆盖正文关键部分，且能避免超长 prompt 导致的空响应/挂起。
SCREENING_MAX_CHARS = 90000

# Temperature for LLM screening (0.0-2.0). Lower = more deterministic.
# 0.1 is recommended for systematic review — ensures reproducible Include/Exclude decisions.
SCREENING_TEMPERATURE = 0.1

# Seconds to wait between LLM API calls
SCREENING_API_DELAY = 2.0
