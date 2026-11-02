import os
import re
import sys
import time
import json
import base64
import pathlib
import requests
import datetime
import threading
import yt_dlp
from urllib.parse import urlparse, unquote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

CONFIG_FILE = "config.json"
LOCALES_FILE = "locales.json"
STATE_FILE = "progress_state.json"
COOKIES_FILE = "cookies.txt"
PLAYWRIGHT_STATE = "playwright_state.json"

ABORT_FLAG = threading.Event()

DEFAULT_CONFIG = {
    "language": "pl",
    "download_attachments": True,
    "download_canva_presentations": True,
    "save_canva_as_pdf": True,
    "save_canva_slides_png": True,
    "save_comments_md": True,
    "save_comments_json": True,
    "verify_existing_files": False,
    "create_media_dump": True,
    "create_link_status": True,
    "preserve_sidebar_section_structure": True,
    "group_into_modules": True,
    "headless": False,
    "max_concurrent_downloads": 2,
    "ignored_titles": ["Example ignored lesson"],
    "download_external_videos": False,
    "download_embedded_videos_posts": True,
    "download_embedded_videos_comments": True,
    "download_linked_videos_posts": False,
    "download_linked_videos_comments": False,
    "max_post_name_length": 50,
    "max_category_name_length": 120,
    "backward_compatible_names": True,
    "fallback_username": "Me",
    "download_lesson_thumbnails": False,
    "keep_state_on_completion": True,
    "restore_state_from_local_folders": True
}

LEGACY_CONFIG_MAP = {
    "pobieraj_zalaczniki": "download_attachments",
    "pobieraj_prezentacje_canva": "download_canva_presentations",
    "zapisuj_canva_jako_pdf": "save_canva_as_pdf",
    "zapisuj_canva_slajdy_png": "save_canva_slides_png",
    "zapisuj_komentarze_md": "save_comments_md",
    "zapisuj_komentarze_json": "save_comments_json",
    "tryb_weryfikacji_plikow": "verify_existing_files",
    "tworz_media_dump": "create_media_dump",
    "tworz_status_linkow": "create_link_status",
    "zachowaj_strukture_sekcji_z_paska": "preserve_sidebar_section_structure",
    "grupuj_w_moduly": "group_into_modules",
    "pomijane": "ignored_titles",
    "pobieraj_wideo_zewnetrzne": "download_external_videos",
    "pobieraj_wideo_embed_posty": "download_embedded_videos_posts",
    "pobieraj_wideo_embed_komentarze": "download_embedded_videos_comments",
    "pobieraj_wideo_linki_posty": "download_linked_videos_posts",
    "pobieraj_wideo_linki_komentarze": "download_linked_videos_comments",
    "max_dlugosc_nazwy_posta": "max_post_name_length",
    "max_dlugosc_nazwy_kategorii": "max_category_name_length",
    "wsteczna_kompatybilnosc_nazw": "backward_compatible_names",
    "nazwa_uzytkownika_zastepcza": "fallback_username",
    "pobieraj_miniaturki_lekcji": "download_lesson_thumbnails",
    "zachowaj_stan_po_zakonczeniu": "keep_state_on_completion",
    "odtworz_stan_z_lokalnych_folderow": "restore_state_from_local_folders"
}

OBSOLETE_CONFIG_KEYS = ("save_canva_as_pptx", "zapisuj_canva_jako_pptx", "canva_render_scale")

def load_locales():
    if not os.path.exists(LOCALES_FILE):
        print(f"[!] Critical error: '{LOCALES_FILE}' not found! Make sure it is in the same directory as the script.")
        sys.exit(1)
    try:
        with open(LOCALES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] Critical error while reading '{LOCALES_FILE}': {e}")
        sys.exit(1)

LOCALES = load_locales()

def load_or_create_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        lang = DEFAULT_CONFIG["language"]
        print(LOCALES.get(lang, LOCALES.get("en", {})).get("config_created", "[*] Generated config.json."))
        return DEFAULT_CONFIG.copy()
    
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        loaded_config = json.load(f)
        
    config_updated = False
    for old_key, new_key in LEGACY_CONFIG_MAP.items():
        if old_key in loaded_config:
            if new_key not in loaded_config:
                loaded_config[new_key] = loaded_config[old_key]
            del loaded_config[old_key]
            config_updated = True

    for obs_key in OBSOLETE_CONFIG_KEYS:
        if obs_key in loaded_config:
            del loaded_config[obs_key]
            config_updated = True

    for key, value in DEFAULT_CONFIG.items():
        if key not in loaded_config:
            loaded_config[key] = value
            config_updated = True
            
    if config_updated:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(loaded_config, f, indent=4, ensure_ascii=False)
        lang = loaded_config.get("language", "pl")
        print(LOCALES.get(lang, LOCALES.get("en", {})).get("config_updated", "[*] Updated config.json."))
        
    return loaded_config

CONFIG = load_or_create_config()

def t(key, **kwargs):
    lang = CONFIG.get("language", "pl")
    lang_dict = LOCALES.get(lang) or LOCALES.get("en") or next(iter(LOCALES.values()), {})
    text = lang_dict.get(key) or LOCALES.get("en", {}).get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

class LoggerWriter:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "w", encoding="utf-8")
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        self.error_ledger = []

    def write(self, message):
        self.terminal.write(message)
        clean_msg = self.ansi_escape.sub('', message)
        if '\r' not in clean_msg:
            self.log.write(clean_msg)
        else:
            if '100%' in clean_msg and (' in ' in clean_msg or ' at ' in clean_msg):
                clean_msg = clean_msg.replace('\r', '')
                if not clean_msg.endswith('\n'):
                    clean_msg += '\n'
                self.log.write(clean_msg)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return hasattr(self.terminal, 'isatty') and self.terminal.isatty()
        
    def log_error(self, component, url_or_path, error_msg):
        self.error_ledger.append(f"- [{component}] {url_or_path} : {error_msg}")

log_filename = f"{t('file_console_log_prefix')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
logger_instance = LoggerWriter(log_filename)
sys.stdout = logger_instance
sys.stderr = logger_instance

class StatusTracker:
    def __init__(self, filepath, main_url):
        self.filepath = filepath
        self.main_url = main_url
        self.lessons = {}

    def add_lesson(self, url):
        if url not in self.lessons:
            self.lessons[url] = {"status": t("status_pending"), "files": {}}
    
    def set_lesson_status(self, url, status):
        if url in self.lessons:
            self.lessons[url]["status"] = status
            self.save()

    def add_file(self, lesson_url, file_url, file_type="File"):
        if lesson_url in self.lessons:
            self.lessons[lesson_url]["files"][file_url] = {"status": t("status_pending"), "type": file_type}
            self.save()
    
    def set_file_status(self, lesson_url, file_url, status):
        if lesson_url in self.lessons and file_url in self.lessons[lesson_url]["files"]:
            self.lessons[lesson_url]["files"][file_url]["status"] = status
            self.save()

    def save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(t("status_header", url=self.main_url))
            for l_url, data in self.lessons.items():
                f.write(t("status_lesson_header"))
                f.write(f"{data['status']} {l_url}\n")
                if data["files"]:
                    for f_url, f_data in data["files"].items():
                        f.write(f"  {f_data['status']} {f_data['type']}: {f_url}\n")
                f.write("\n")

class StateManager:
    REQUIRED_STEPS = ("content", "comments", "attachments", "presentations", "transcripts", "videos")
    LEGACY_STEP_MAP = {
        "tresc": "content",
        "komentarze": "comments",
        "zalaczniki": "attachments",
        "prezentacje": "presentations",
        "transkrypty": "transcripts",
        "wideo": "videos"
    }

    def __init__(self):
        self.state = self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "completed_urls" in data:
                        for url, steps in data["completed_urls"].items():
                            for old_k, new_k in self.LEGACY_STEP_MAP.items():
                                if old_k in steps:
                                    if new_k not in steps:
                                        steps[new_k] = steps[old_k]
                                    del steps[old_k]
                        return data
            except Exception:
                pass
        return {"completed_urls": {}}

    def save_state(self):
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=4, ensure_ascii=False)

    def init_url(self, url):
        if url not in self.state["completed_urls"]:
            self.state["completed_urls"][url] = {step: False for step in self.REQUIRED_STEPS}
        else:
            for step in self.REQUIRED_STEPS:
                if step not in self.state["completed_urls"][url]:
                    self.state["completed_urls"][url][step] = False

    def mark_step_done(self, url, step):
        self.init_url(url)
        self.state["completed_urls"][url][step] = True
        self.save_state()

    def mark_step_undone(self, url, step):
        self.init_url(url)
        self.state["completed_urls"][url][step] = False
        self.save_state()

    def mark_all_done(self, url):
        self.init_url(url)
        for step in self.REQUIRED_STEPS:
            self.state["completed_urls"][url][step] = True
        self.save_state()

    def is_step_done(self, url, step):
        return self.state["completed_urls"].get(url, {}).get(step, False)

    def is_url_fully_done(self, url):
        steps = self.state["completed_urls"].get(url, {})
        if not steps:
            return False
        return all(steps.get(s, False) for s in self.REQUIRED_STEPS)

    def clear_state(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

def restore_state_from_disk(state_mgr):
    if not CONFIG.get("restore_state_from_local_folders", True):
        return
    
    print(t("Scanning_local_folders"))
    restored_count = 0
    content_suffixes = ("tresc.md", "content.md", t("file_content_suffix"))
    meta_suffixes = content_suffixes + ("media_dump.md", t("file_media_dump_suffix"))
    canva_notes_suffixes = ("tekst_i_zrodla.md", "text_and_sources.md", t("file_canva_notes_suffix"))
    
    for root, dirs, files in os.walk(os.getcwd()):
        found_url = None
        meta_files = [f for f in files if f.endswith(meta_suffixes)]
        if not any(f.endswith(content_suffixes) for f in meta_files):
            continue
            
        has_canva_embed_in_content = False
        for file in meta_files:
            filepath = os.path.join(root, file)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read(8192)
                    if not found_url:
                        match = re.search(r'\*\*(?:Oryginalny link|Original link|' + re.escape(t("md_original_link")) + r'):\*\*\s*(https?://[^\s<]+)', content)
                        if match:
                            found_url = match.group(1).strip()
                    if "canva.com/design/" in content:
                        has_canva_embed_in_content = True
            except Exception:
                pass
                
        if not found_url:
            continue

        incomplete_canva = False
        canva_dirs = [d for d in dirs if "Prezentacja_Canva" in d or "Canva_Presentation" in d or t("file_canva_folder_prefix") in d]
        if canva_dirs:
            for cd in canva_dirs:
                cd_path = os.path.join(root, cd)
                cd_files = os.listdir(cd_path) if os.path.exists(cd_path) else []
                has_md = any(f.endswith(canva_notes_suffixes) for f in cd_files)
                has_pdf = (not CONFIG.get("save_canva_as_pdf", True)) or any(f.endswith(".pdf") for f in cd_files)
                if not (has_md and has_pdf):
                    incomplete_canva = True
                    break
        elif has_canva_embed_in_content and CONFIG.get("download_canva_presentations", True):
            incomplete_canva = True

        if found_url not in state_mgr.state["completed_urls"]:
            state_mgr.mark_all_done(found_url)
            if incomplete_canva:
                state_mgr.mark_step_undone(found_url, "presentations")
            else:
                restored_count += 1
        elif incomplete_canva and state_mgr.is_step_done(found_url, "presentations"):
            state_mgr.mark_step_undone(found_url, "presentations")
                    
    if restored_count > 0:
        print(t("restored_history_count", count=restored_count))
    else:
        print(t("scanning_finished"))

def sanitize_name(name, limit=None):
    if not name:
        return t("default_no_name")
    
    name = name.replace('\n', ' ').replace('\r', ' ')
    clean = re.sub(r'[\\/*?:"<>|„””’‘\x00-\x1f]', "", name).strip()
    clean = re.sub(r'\s+', ' ', clean)
    
    if limit and len(clean) > limit:
        clean = clean[:limit].strip()
        
    return clean.rstrip(". ") or t("default_no_name")

def build_compatible_path(base_dir, elements, is_file=False):
    current_path = base_dir
    for i, element in enumerate(elements):
        is_target_file = is_file and (i == len(elements) - 1)
        
        if not os.path.exists(current_path) or not CONFIG.get("backward_compatible_names", True):
            current_path = os.path.join(current_path, element)
            continue
        
        matched_element = element
        target_base, target_ext = os.path.splitext(element) if is_target_file else (element, "")
        
        found_exact = False
        for existing_item in os.listdir(current_path):
            item_path = os.path.join(current_path, existing_item)
            if not is_target_file and not os.path.isdir(item_path): continue
            if is_target_file and not os.path.isfile(item_path): continue
            
            if existing_item == element:
                matched_element = existing_item
                found_exact = True
                break
        
        if not found_exact:
            for existing_item in os.listdir(current_path):
                item_path = os.path.join(current_path, existing_item)
                if not is_target_file and not os.path.isdir(item_path): continue
                if is_target_file and not os.path.isfile(item_path): continue
                
                existing_base, existing_ext = os.path.splitext(existing_item) if is_target_file else (existing_item, "")
                
                if existing_ext.lower() == target_ext.lower():
                    if target_base.startswith(existing_base) or existing_base.startswith(target_base):
                        shorter = min(len(existing_base), len(target_base))
                        if shorter >= 25: 
                            matched_element = existing_item
                            break
        
        current_path = os.path.join(current_path, matched_element)
    
    return current_path

def calculate_relative_date(text_lower, now):
    if any(x in text_lower for x in ["just now", "right now", "przed chwila", "przed chwilą", "teraz", "dzisiaj", "today"]):
        return now
    if any(x in text_lower for x in ["wczoraj", "yesterday"]):
        return now - datetime.timedelta(days=1)

    m_sec = re.search(r'(\d+)\s*(?:s|sec|secs|second|seconds|sek|sekund[ay]?)\b', text_lower)
    if m_sec:
        return now - datetime.timedelta(seconds=int(m_sec.group(1)))

    m_min = re.search(r'(\d+)\s*(?:m|min|mins|minute|minutes|minut[ay]?)\b', text_lower)
    if m_min:
        return now - datetime.timedelta(minutes=int(m_min.group(1)))

    m_hr = re.search(r'(\d+)\s*(?:h|hr|hrs|hour|hours|godz|godzin[ay]?)\b', text_lower)
    if m_hr:
        return now - datetime.timedelta(hours=int(m_hr.group(1)))

    m_day = re.search(r'(\d+)\s*(?:d|day|days|dni|dzień|dzien)\b', text_lower)
    if m_day:
        return now - datetime.timedelta(days=int(m_day.group(1)))

    m_wk = re.search(r'(\d+)\s*(?:w|wk|wks|week|weeks|tyg|tydzień|tydzien|tygodni[ea]?)\b', text_lower)
    if m_wk:
        return now - datetime.timedelta(weeks=int(m_wk.group(1)))

    m_mo = re.search(r'(\d+)\s*(?:mo|mos|month|months|mies|miesiąc|miesiac|miesięcy|miesiecy)\b', text_lower)
    if m_mo:
        return now - datetime.timedelta(days=int(m_mo.group(1)) * 30)

    m_yr = re.search(r'(\d+)\s*(?:y|yr|yrs|year|years|rok|lat|lata)\b', text_lower)
    if m_yr:
        return now - datetime.timedelta(days=int(m_yr.group(1)) * 365)

    if any(x in text_lower for x in ["ago", "temu", "sekund", "minut", "godzin", "now"]):
        return now

    return None

def parse_date(raw_text):
    text = str(raw_text).lower().strip()
    if not text or text in ["brak daty", "no date", "none", t("default_no_date").lower()]:
        return "0000-00-00"
        
    if re.match(r'^\d{4}-\d{2}-\d{2}$', text):
        return text
        
    now = datetime.datetime.now()
    relative_date = calculate_relative_date(text, now)
    if relative_date:
        return relative_date.strftime("%Y-%m-%d")
        
    months = {
        'jan': 1, 'sty': 1, 'feb': 2, 'lut': 2, 'mar': 3,
        'apr': 4, 'kwi': 4, 'may': 5, 'maj': 5, 'jun': 6, 'cze': 6,
        'jul': 7, 'lip': 7, 'aug': 8, 'sie': 8, 'sep': 9, 'wrz': 9,
        'oct': 10, 'paź': 10, 'paz': 10, 'nov': 11, 'lis': 11,
        'dec': 12, 'gru': 12
    }
    
    m_year = re.search(r'\b(20\d{2})\b', text)
    year = int(m_year.group(1)) if m_year else now.year
        
    text_without_year = re.sub(r'\b(20\d{2})\b', '', text)
    m_day = re.search(r'\b(\d{1,2})\b', text_without_year)
    day = int(m_day.group(1)) if m_day else 1
        
    month = None
    for k, v in months.items():
        if k in text:
            month = v
            break
            
    if month is None:
        return "0000-00-00"

    if not m_year:
        try:
            candidate = datetime.datetime(year, month, day)
            if candidate > now + datetime.timedelta(days=2):
                year -= 1
        except ValueError:
            pass
            
    return f"{year:04d}-{month:02d}-{day:02d}"

def parse_comment_date(raw_text):
    text = str(raw_text).strip()
    text_lower = text.lower()
    if not text or text_lower in ["brak daty", "no date", "none", t("default_no_date").lower()]:
        return t("default_no_date")
        
    now = datetime.datetime.now()
    
    time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)', text)
    time_str = f" {time_match.group(1).strip()}" if time_match else ""
    
    text_without_time = re.sub(r'\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\b', '', text_lower).strip()
    relative_date = calculate_relative_date(text_without_time, now)
    if relative_date:
        return relative_date.strftime("%Y-%m-%d") + time_str
        
    months = {
        'jan': 1, 'sty': 1, 'feb': 2, 'lut': 2, 'mar': 3,
        'apr': 4, 'kwi': 4, 'may': 5, 'maj': 5, 'jun': 6, 'cze': 6,
        'jul': 7, 'lip': 7, 'aug': 8, 'sie': 8, 'sep': 9, 'wrz': 9,
        'oct': 10, 'paź': 10, 'paz': 10, 'nov': 11, 'lis': 11, 'dec': 12, 'gru': 12
    }
    
    m_year = re.search(r'\b(20\d{2})\b', text_without_time)
    year = int(m_year.group(1)) if m_year else now.year
        
    m_day = re.search(r'\b(\d{1,2})\b', re.sub(r'\b(20\d{2})\b', '', text_without_time))
    if not m_day:
        if time_str:
            return now.strftime("%Y-%m-%d") + time_str
        return text
        
    day = int(m_day.group(1))
        
    month = None
    for k, v in months.items():
        if k in text_lower:
            month = v
            break
    
    if month:
        if not m_year:
            try:
                candidate = datetime.datetime(year, month, day)
                if candidate > now + datetime.timedelta(days=2):
                    year -= 1
            except ValueError:
                pass
        return f"{year:04d}-{month:02d}-{day:02d}{time_str}"
    return text

def export_cookies_to_netscape(cookies, filename=COOKIES_FILE):
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c['domain']
            include_subdom = "TRUE" if domain.startswith('.') else "FALSE"
            path = c['path']
            secure = "TRUE" if c['secure'] else "FALSE"
            expires = int(c['expires']) if c.get('expires') and c['expires'] > 0 else int(time.time()) + 3600
            name = c['name']
            value = c['value']
            f.write(f"{domain}\t{include_subdom}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")

def safe_goto(page, url, css_verifier=None):
    delay = 5
    max_delay = 120 
    while True:
        try:
            response = page.goto(url, timeout=60000)
            if response and response.status >= 500:
                print(t("server_error_retry", status=response.status, delay=delay))
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
                continue
                
            try:
                page.wait_for_load_state('domcontentloaded', timeout=15000)
                page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass 
            
            is_ok = True
            if css_verifier:
                if page.locator(css_verifier).count() == 0:
                    is_ok = False
                    
            if is_ok:
                return 
            else:
                print(t("incomplete_page_retry", delay=delay))
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
                
        except Exception:
            print(t("connection_error_retry", delay=delay))
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

def get_extension_from_content_type(content_type):
    if not content_type:
        return ""
    content_type = content_type.lower()
    mapping = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'image/svg+xml': '.svg',
        'application/pdf': '.pdf',
        'application/zip': '.zip',
        'application/x-zip-compressed': '.zip',
        'application/x-rar-compressed': '.rar',
        'application/vnd.rar': '.rar',
        'application/x-7z-compressed': '.7z',
        'audio/mpeg': '.mp3',
        'audio/wav': '.wav',
        'audio/ogg': '.ogg',
        'video/mp4': '.mp4',
        'video/webm': '.webm',
        'text/plain': '.txt',
        'text/csv': '.csv',
        'application/json': '.json',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        'application/vnd.ms-powerpoint': '.ppt',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx'
    }
    for mime, ext in mapping.items():
        if mime in content_type:
            return ext
    return ""

def download_file_with_resume(url, filepath, cookies_dict, referer_url=None):
    delay = 5
    max_delay = 120
    max_retries = 5
    retries = 0
    
    base_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
    }
    if referer_url:
        base_headers['Referer'] = referer_url
    
    while retries < max_retries:
        if ABORT_FLAG.is_set():
            return False
            
        headers = base_headers.copy()
        try:
            head_resp = requests.head(url, cookies=cookies_dict, headers=headers, allow_redirects=True, timeout=15)
            
            if head_resp.status_code in [400, 401, 403, 404, 405, 410]:
                if head_resp.status_code != 405:
                    msg = t("permanent_server_error", status=head_resp.status_code)
                    print(t("permanent_error_skip", msg=msg, filename=os.path.basename(filepath)))
                    sys.stdout.log_error("Attachment", url, msg)
                    return False
                server_size = 0
                content_type = ""
            else:
                server_size = int(head_resp.headers.get('Content-Length', 0))
                content_type = head_resp.headers.get('Content-Type', '')
            
            ext = get_extension_from_content_type(content_type)
            current_ext = os.path.splitext(filepath)[1].lower()
            if ext and not current_ext:
                filepath = filepath + ext
            elif ext == '.gif' and current_ext != '.gif':
                filepath = filepath + ext
            
            if CONFIG["verify_existing_files"] or os.path.exists(filepath):
                if os.path.exists(filepath):
                    local_size = os.path.getsize(filepath)
                    if server_size > 0:
                        if local_size == server_size:
                            print(t("file_complete", filename=os.path.basename(filepath)))
                            return True
                        elif local_size < server_size:
                            print(t("resuming_download", filename=os.path.basename(filepath), local=local_size, server=server_size))
                            headers['Range'] = f'bytes={local_size}-'
                        else:
                            os.remove(filepath)
                    elif local_size > 0 and not CONFIG["verify_existing_files"]:
                        print(t("file_exists", filename=os.path.basename(filepath)))
                        return True

            mode = 'ab' if 'Range' in headers else 'wb'
            with requests.get(url, cookies=cookies_dict, headers=headers, stream=True, timeout=30) as r:
                if r.status_code in [400, 401, 403, 404, 405, 410]:
                    msg = t("access_error", status=r.status_code)
                    print(t("access_error_skip", msg=msg, filename=os.path.basename(filepath)))
                    sys.stdout.log_error("Attachment", url, msg)
                    return False
                    
                r.raise_for_status()
                if not ext:
                    get_ext = get_extension_from_content_type(r.headers.get('Content-Type', ''))
                    if get_ext and not os.path.splitext(filepath)[1]:
                        filepath = filepath + get_ext
                        
                with open(filepath, mode) as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if ABORT_FLAG.is_set():
                            return False
                        if chunk:
                            f.write(chunk)
            print(t("download_finished", filename=os.path.basename(filepath)))
            return True
            
        except Exception:
            pass
            
        retries += 1
        if retries >= max_retries:
            msg = t("max_retries_exceeded")
            print(t("max_retries_skip", msg=msg, filename=os.path.basename(filepath)))
            sys.stdout.log_error("Attachment", url, msg)
            return False
            
        print(t("download_interrupted_retry", filename=os.path.basename(filepath), delay=delay))
        if ABORT_FLAG.wait(delay):
            return False
        delay = min(delay * 2, max_delay)
            
    return False

def expand_full_content(page):
    try:
        btns = page.locator('button[data-testid="see-more-post"], button[data-testid="show-expand"], button:has-text("See more"), button:has-text("Pokaż więcej")')
        for i in range(btns.count()):
            btn = btns.nth(i)
            if btn.is_visible():
                try:
                    btn.click(timeout=1500)
                    time.sleep(0.3)
                except Exception:
                    pass
    except Exception:
        pass

def scroll_to_bottom(page, selector, item_type="items", is_comments=False):
    print(t("scrolling_page", item_type=item_type))

    try:
        page.locator('body').focus()
    except Exception:
        pass

    page.mouse.move(500, 500)
    
    max_empty_attempts = 4
    attempts = 0
    
    page.keyboard.press("End")
    time.sleep(1.5)

    while attempts < max_empty_attempts:
        count_before = page.locator(selector).count()
        
        page.keyboard.press("PageDown")
        page.keyboard.press("End")
        page.mouse.wheel(0, 5000)
        
        try:
            page.evaluate('''(sel) => {
                document.querySelectorAll('#message-scroll-view, #message-thread-scroll-view, #circle-ai-workspace-body, [data-slot="scroll-area-viewport"]').forEach(el => {
                    el.scrollTop = el.scrollHeight;
                });
                let items = document.querySelectorAll(sel);
                if (items.length > 0) {
                    items[items.length - 1].scrollIntoView({behavior: "instant", block: "end"});
                }
            }''', selector)
        except Exception:
            pass
        
        for _ in range(20):
            time.sleep(0.3)
            if page.locator(selector).count() > count_before:
                break
        
        count_after = page.locator(selector).count()
        
        if count_after > count_before:
            attempts = 0
        else:
            attempts += 1 
            if count_after == 0 and attempts >= 2:
                break
            if attempts < max_empty_attempts:
                print(t("waiting_for_load", attempt=attempts, max_attempts=max_empty_attempts))

    if is_comments:
        max_button_clicks = 30
        click_attempts = 0
        while click_attempts < max_button_clicks:
            expand_buttons = page.locator("button:has-text('Show more replies'), button:has-text('Pokaż więcej odpowiedzi'), button:has-text('Load more'), button:has-text('Załaduj więcej')")
            visible_count = 0
            for i in range(expand_buttons.count()):
                if expand_buttons.nth(i).is_visible():
                    try:
                        expand_buttons.nth(i).click(position={"x": 5, "y": 5}, timeout=2000)
                        visible_count += 1
                        time.sleep(0.5)
                    except Exception:
                        pass
            if visible_count == 0:
                break
            click_attempts += 1
        expand_full_content(page)

def switch_tab(page, tab_names):
    for name in tab_names:
        tab = page.locator(f'button[role="tab"]:has-text("{name}")')
        if tab.count() > 0 and tab.first.is_visible():
            if tab.first.get_attribute("aria-selected") != "true":
                try:
                    tab.first.click(timeout=3000)
                    time.sleep(1.5)
                except Exception:
                    pass
            return True
    return False

def scrape_visible_comments(page, actual_username):
    raw_comments = page.evaluate('''() => {
        let results = [];
        document.querySelectorAll('.post--comment, [data-testid="message-item"], article[data-circle="comment"], div[data-circle="comment"]').forEach(node => {
            let author = "Unknown";
            let content = "";
            let dateRaw = "No date";
            
            let nameNode = node.querySelector('[data-circle="comment-author-name"]') || node.querySelector('.author__name a') || node.querySelector('[data-testid="open-profile-drawer"]');
            if (nameNode) author = nameNode.innerText.trim();
            
            if (author.toLowerCase() === "you" || author.toLowerCase() === "ty") {
                let avatarRole = node.querySelector('[role="img"][aria-label]');
                if (avatarRole && avatarRole.getAttribute('aria-label')) {
                    let candidate = avatarRole.getAttribute('aria-label').trim();
                    if (candidate && candidate.toLowerCase() !== "you") {
                        author = candidate;
                    }
                }
            }
            
            let bodyNode = node.querySelector('[data-circle="comment-body"]') || node.querySelector('[data-circle="rich-text-content"]') || node.querySelector('[data-testid="comment-body-inside"] .tiptap') || node.querySelector('[data-testid="message-text"]');
            if (bodyNode) content = bodyNode.innerText.trim();
            
            let timeNode = node.querySelector('[data-circle="comment-author-timestamp"]') || node.querySelector('.text-timestamp') || node.querySelector('span.text-caption.text-tertiary') || node.querySelector('.author__time span') || node.querySelector('.ago') || node.querySelector('time');
            if (timeNode) {
                let timeText = timeNode.innerText.trim();
                let prev = node.previousElementSibling;
                let datePrefix = "";
                while (prev) {
                    let dateSpan = prev.querySelector('.uppercase, span.text-label-xs.text-tertiary');
                    if (dateSpan && !prev.hasAttribute('data-testid')) {
                        let txt = dateSpan.innerText.trim();
                        if (txt && !txt.includes('replies') && !txt.includes('odpowiedzi')) {
                            datePrefix = txt;
                            break;
                        }
                    }
                    prev = prev.previousElementSibling;
                }
                dateRaw = (datePrefix && !timeText.toLowerCase().includes(datePrefix.toLowerCase())) ? (datePrefix + " " + timeText) : timeText;
            }
            
            let aEl = node.querySelector('.author__time a[title]');
            if (aEl && aEl.getAttribute('title')) {
                dateRaw = aEl.getAttribute('title').trim();
            }
            
            results.push({author: author, content: content, date_raw: dateRaw});
        });
        return results;
    }''')
    
    parsed_comments = []
    for item in raw_comments:
        author = item['author']
        if author == "Unknown":
            author = t("default_unknown_author")
        elif author.lower() in ["you", "ty"]:
            author = actual_username
            
        date_formatted = parse_comment_date(item['date_raw'])
        if CONFIG.get("language", "pl") == "pl":
            parsed_comments.append({"autor": author, "data": date_formatted, "tresc": item['content']})
        else:
            parsed_comments.append({"author": author, "date": date_formatted, "content": item['content']})
    return parsed_comments

def save_slides_as_pdf(slides_bundle, pdf_path, playwright_instance, presentation_folder, slide_width=1920, slide_height=1080):
    if not slides_bundle:
        return False

    temp_html_path = os.path.join(presentation_folder, "_temp_canva_pdf_render.html")
    temp_img_paths = []
    temp_browser = None
    try:
        pages_html = []
        for idx, slide in enumerate(slides_bundle, start=1):
            img_path = slide.get("img_path")
            if not img_path or not os.path.exists(img_path):
                img_path = os.path.join(presentation_folder, f"_temp_slide_{idx:03d}.png")
                with open(img_path, "wb") as f_tmp:
                    f_tmp.write(slide["png"])
                temp_img_paths.append(img_path)

            img_filename = os.path.basename(img_path)
            overlays = []
            for lnk in slide.get("links", []):
                url_safe = lnk["url"].replace('"', '&quot;')
                left_pct = max(0.0, min(99.0, float(lnk.get("x_pct", 0.0))))
                top_pct = max(0.0, min(99.0, float(lnk.get("y_pct", 0.0))))
                w_pct = max(1.0, min(100.0 - left_pct, float(lnk.get("w_pct", 10.0))))
                h_pct = max(1.0, min(100.0 - top_pct, float(lnk.get("h_pct", 5.0))))
                overlays.append(
                    f'<a href="{url_safe}" target="_blank" style="position:absolute; left:{left_pct:.4f}%; top:{top_pct:.4f}%; width:{w_pct:.4f}%; height:{h_pct:.4f}%; display:block; z-index:10; text-decoration:none; overflow:hidden; background:rgba(0,0,0,0);">'
                    f'<span style="display:block; width:100%; height:100%; font-size:200px; line-height:1; color:transparent; opacity:0.001;">LINK</span>'
                    f'</a>'
                )
            pages_html.append(
                f'<div class="page"><img src="{img_filename}">{"".join(overlays)}</div>'
            )

        html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    @page {{ size: {slide_width}px {slide_height}px; margin: 0; }}
    body {{ margin: 0; padding: 0; background: #000; }}
    .page {{ position: relative; width: {slide_width}px; height: {slide_height}px; page-break-after: always; overflow: hidden; }}
    .page:last-child {{ page-break-after: auto; }}
    img {{ width: {slide_width}px; height: {slide_height}px; object-fit: fill; display: block; }}
</style>
</head>
<body>{''.join(pages_html)}</body>
</html>"""

        with open(temp_html_path, "w", encoding="utf-8") as f_html:
            f_html.write(html_doc)

        temp_browser = playwright_instance.chromium.launch(headless=True)
        temp_page = temp_browser.new_page()
        temp_page.goto(pathlib.Path(temp_html_path).resolve().as_uri(), wait_until="load", timeout=120000)
        temp_page.pdf(path=pdf_path, width=f"{slide_width}px", height=f"{slide_height}px", print_background=True)
        temp_browser.close()
        temp_browser = None
        return True
    except Exception as e:
        print(t("pdf_generation_failed", error=e))
    finally:
        if temp_browser:
            try:
                temp_browser.close()
            except Exception:
                pass
        if os.path.exists(temp_html_path):
            try:
                os.remove(temp_html_path)
            except Exception:
                pass
        for p_tmp in temp_img_paths:
            if os.path.exists(p_tmp):
                try:
                    os.remove(p_tmp)
                except Exception:
                    pass

    try:
        from PIL import Image
        import io
        images = []
        for slide in slides_bundle:
            img = Image.open(io.BytesIO(slide["png"]))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            images.append(img)
        if images:
            images[0].save(pdf_path, "PDF", resolution=100.0, save_all=True, append_images=images[1:])
            return True
    except Exception:
        pass

    return False

def download_canva_presentations(page, context, playwright_instance, target_folder, file_prefix, full_url, tracker):
    canva_raw_urls = page.evaluate('''() => {
        let urls = [];
        document.querySelectorAll('iframe[src*="canva.com/design/"], a[href*="canva.com/design/"]').forEach(el => {
            let u = el.getAttribute('src') || el.getAttribute('href');
            if (u && !urls.includes(u)) urls.push(u);
        });
        return urls;
    }''')
    
    if not canva_raw_urls:
        return True

    unique_canva_urls = []
    for u in canva_raw_urls:
        if u.startswith('//'):
            u = 'https:' + u
        m = re.search(r'(https?://(?:www\.)?canva\.com/design/[^/?#]+/[^/?#]+)', u)
        clean_u = (m.group(1) + "/view?embed") if m else u
        if clean_u not in unique_canva_urls:
            unique_canva_urls.append(clean_u)

    all_ok = True
    for idx_pres, canva_url in enumerate(unique_canva_urls, start=1):
        if ABORT_FLAG.is_set():
            return False

        if tracker:
            tracker.add_file(full_url, canva_url, f"{t('md_canva_title')} {idx_pres}")

        subfolder_name = f"{file_prefix}{t('file_canva_folder_prefix')}_{idx_pres}"
        presentation_folder = build_compatible_path(target_folder, [subfolder_name], is_file=False)
        os.makedirs(presentation_folder, exist_ok=True)

        pdf_path = os.path.join(presentation_folder, f"{subfolder_name}.pdf")
        md_path = os.path.join(presentation_folder, f"{subfolder_name}_{t('file_canva_notes_suffix')}")

        if os.path.exists(md_path) and (not CONFIG.get("save_canva_as_pdf", True) or os.path.exists(pdf_path)):
            print(t("canva_complete", name=subfolder_name))
            if tracker:
                tracker.set_file_status(full_url, canva_url, "[X]")
            continue

        print(t("canva_downloading", idx=idx_pres, total=len(unique_canva_urls), url=canva_url))
        pres_page = None
        try:
            pres_page = context.new_page()
            pres_page.set_viewport_size({"width": 1920, "height": 1080})
            pres_page.goto(canva_url, referer=full_url, timeout=60000)
            try:
                pres_page.wait_for_load_state('domcontentloaded', timeout=15000)
                pres_page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
            time.sleep(3.0)

            bootstrap_meta = pres_page.evaluate('''() => {
                let width = 1920;
                let height = 1080;
                let title = "";
                let totalSlides = null;
                let bootstrapSlides = [];
                try {
                    let b = window.bootstrap;
                    if (b && b.page && b.page.C && b.page.C.D && b.page.C.D.A) {
                        let d = b.page.C.D.A;
                        if (d.C && d.C.A && d.C.B) {
                            width = Math.round(d.C.A);
                            height = Math.round(d.C.B);
                        }
                        if (d.D) title = d.D;
                        if (Array.isArray(d.A)) {
                            totalSlides = d.A.length;
                            d.A.forEach(s => {
                                let sLinks = [];
                                let sTexts = [];
                                if (Array.isArray(s.E)) {
                                    s.E.forEach(el => {
                                        if (el["A?"] === "O" && el.a) {
                                            sLinks.push({
                                                url: el.a,
                                                text: el.a,
                                                top: el.A || 0,
                                                left: el.B || 0,
                                                width: el.D || 200,
                                                height: el.C || 80
                                            });
                                        }
                                        if (el.f && Array.isArray(el.f)) {
                                            el.f.forEach(fItem => {
                                                if (fItem.A && fItem.A.C && Array.isArray(fItem.A.C.A)) {
                                                    fItem.A.C.A.forEach(tStr => {
                                                        let cleanT = tStr.trim();
                                                        if (cleanT && !sTexts.includes(cleanT)) sTexts.push(cleanT);
                                                    });
                                                }
                                            });
                                        }
                                        if (el.a && el.a.C && Array.isArray(el.a.C.A)) {
                                            el.a.C.A.forEach(tStr => {
                                                let cleanT = tStr.trim();
                                                if (cleanT && !sTexts.includes(cleanT)) sTexts.push(cleanT);
                                            });
                                        }
                                    });
                                }
                                bootstrapSlides.push({links: sLinks, texts: sTexts});
                            });
                        }
                    }
                } catch(e) {}
                return {width: width, height: height, title: title, totalSlides: totalSlides, bootstrapSlides: bootstrapSlides};
            }''')

            native_w = max(320, min(3840, int(bootstrap_meta.get("width") or 1920)))
            native_h = max(240, min(3840, int(bootstrap_meta.get("height") or 1080)))
            orig_deck_title = bootstrap_meta.get("title") or ""
            bootstrap_slides = bootstrap_meta.get("bootstrapSlides") or []

            if native_w != 1920 or native_h != 1080:
                pres_page.set_viewport_size({"width": native_w, "height": native_h})
                time.sleep(0.5)

            for _ in range(10):
                current_pos = pres_page.evaluate('''() => {
                    let slider = document.querySelector('[role="slider"][aria-valuenow]');
                    return slider ? parseInt(slider.getAttribute('aria-valuenow')) : 1;
                }''')
                if current_pos <= 1:
                    break
                prev_btn = pres_page.locator('button[aria-label="Previous page"], button[aria-label*="Poprzedn"]').first
                if prev_btn.count() > 0:
                    prev_btn.click(timeout=2000)
                else:
                    pres_page.keyboard.press("ArrowLeft")
                time.sleep(0.4)

            slides_bundle = []
            md_description = f"# {t('md_canva_title')} {idx_pres}"
            if orig_deck_title:
                md_description += f" ({orig_deck_title})"
            md_description += f"\n**{t('md_canva_source')}:** {canva_url}\n**{t('md_canva_parent_post')}:** {full_url}\n\n"
            all_sources = []
            previous_png = None
            max_slides = max(250, (bootstrap_meta.get("totalSlides") or 200) + 15)

            for slide_num in range(1, max_slides + 1):
                if ABORT_FLAG.is_set():
                    pres_page.close()
                    return False

                try:
                    pres_page.wait_for_load_state('networkidle', timeout=2500)
                except Exception:
                    pass
                time.sleep(0.8)

                nav_state = pres_page.evaluate('''() => {
                    let pageInfo = null;
                    let slider = document.querySelector('[role="slider"][aria-valuenow][aria-valuemax]');
                    if (slider) {
                        pageInfo = {
                            current: parseInt(slider.getAttribute('aria-valuenow')),
                            total: parseInt(slider.getAttribute('aria-valuemax'))
                        };
                    } else {
                        let announcer = document.querySelector('[data-announcer-region="polite"]');
                        if (announcer && announcer.innerText) {
                            let m = announcer.innerText.match(/(\\d+)\\s*(?:of|z|\\/)\\s*(\\d+)/i);
                            if (m) pageInfo = {current: parseInt(m[1]), total: parseInt(m[2])};
                        }
                    }
                    if (!pageInfo) {
                        document.querySelectorAll('span, div').forEach(el => {
                            if (el.children.length === 0 && el.innerText) {
                                let m = el.innerText.trim().match(/^(\\d+)\\s*(?:\\/|of|z)\\s*(\\d+)$/i);
                                if (m) pageInfo = {current: parseInt(m[1]), total: parseInt(m[2])};
                            }
                        });
                    }

                    let nextBtn = document.querySelector('button[aria-label="Next page"], button[aria-label*="Next"], button[aria-label*="Następn"], button[aria-label*="Dalej"], [role="button"][aria-label*="Next"]');
                    let hasNext = true;
                    if (nextBtn) {
                        if (nextBtn.disabled || nextBtn.getAttribute('aria-disabled') === 'true') {
                            hasNext = false;
                        }
                    }
                    return {pageInfo: pageInfo, hasNext: hasNext};
                }''')

                pres_page.evaluate('''() => {
                    let styleEl = document.getElementById('__canva_fullscreen_capture__');
                    if (!styleEl) {
                        styleEl = document.createElement('style');
                        styleEl.id = '__canva_fullscreen_capture__';
                        styleEl.innerHTML = 'footer, nav, header, .BFiqnw { display: none !important; }';
                        document.head.appendChild(styleEl);
                    }
                    document.querySelectorAll('button, [role="button"], [role="navigation"]').forEach(el => {
                        if (!el.closest('a[href]')) {
                            el.dataset.origVis = el.style.visibility || '';
                            el.style.visibility = 'hidden';
                        }
                    });
                }''')
                time.sleep(0.15)

                slide_data = pres_page.evaluate('''() => {
                    let vw = window.innerWidth || 1920;
                    let vh = window.innerHeight || 1080;
                    let activeSlide = document.querySelector('.fMSICA[aria-hidden="false"]') || document.body;
                    let slideCanvas = activeSlide.querySelector('._mXnjA') || activeSlide;
                    let canvasRect = slideCanvas.getBoundingClientRect();

                    let texts = [];
                    let links = [];
                    let seenUrls = new Set();
                    
                    activeSlide.querySelectorAll('a[href]').forEach(a => {
                        if (a.closest('[aria-hidden="true"]')) return;
                        let href = a.getAttribute('href');
                        if (href && !href.startsWith('javascript:') && !href.includes('canva.com/help') && !href.includes('canva.com/policies') && !href.includes('canva.com/login')) {
                            let txt = (a.innerText || a.getAttribute('aria-label') || href).trim().replace(/\\s+/g, ' ');
                            let r = a.getBoundingClientRect();
                            let xPct = r.width > 0 ? (r.left / vw) * 100.0 : 5.0;
                            let yPct = r.height > 0 ? (r.top / vh) * 100.0 : 85.0;
                            let wPct = r.width > 0 ? (r.width / vw) * 100.0 : 25.0;
                            let hPct = r.height > 0 ? (r.height / vh) * 100.0 : 5.0;
                            links.push({
                                text: txt || href,
                                url: href,
                                x_pct: xPct,
                                y_pct: yPct,
                                w_pct: wPct,
                                h_pct: hPct
                            });
                            seenUrls.add(href);
                        }
                    });

                    let walker = document.createTreeWalker(activeSlide, NodeFilter.SHOW_TEXT, null, false);
                    let node;
                    while (node = walker.nextNode()) {
                        let parent = node.parentElement;
                        if (!parent) continue;
                        if (parent.closest('nav, footer, header, script, style, button, [role="navigation"], [aria-hidden="true"]')) continue;
                        let val = node.nodeValue.trim();
                        if (val && val.length > 1 && !texts.includes(val)) {
                            texts.push(val);
                        }
                    }

                    return {
                        texts: texts,
                        links: links,
                        canvasRect: {
                            left: canvasRect.left,
                            top: canvasRect.top,
                            width: canvasRect.width || vw,
                            height: canvasRect.height || vh
                        },
                        vw: vw,
                        vh: vh
                    };
                }''')

                png_bytes = pres_page.screenshot(type="png")

                pres_page.evaluate('''() => {
                    let styleEl = document.getElementById('__canva_fullscreen_capture__');
                    if (styleEl) styleEl.remove();
                    document.querySelectorAll('[data-orig-vis]').forEach(el => {
                        el.style.visibility = el.dataset.origVis;
                    });
                }''')

                page_info = nav_state.get("pageInfo")
                has_next = nav_state.get("hasNext", True)

                current_slide_idx = (page_info or {}).get("current", slide_num) - 1
                if 0 <= current_slide_idx < len(bootstrap_slides):
                    b_slide = bootstrap_slides[current_slide_idx]
                    c_rect = slide_data.get("canvasRect", {"left": 0, "top": 0, "width": native_w, "height": native_h})
                    vw = float(slide_data.get("vw") or native_w)
                    vh = float(slide_data.get("vh") or native_h)
                    existing_urls = {l["url"] for l in slide_data["links"]}
                    for b_lnk in b_slide.get("links", []):
                        if b_lnk["url"] not in existing_urls:
                            screen_x = c_rect["left"] + (float(b_lnk["left"]) / float(native_w)) * c_rect["width"]
                            screen_y = c_rect["top"] + (float(b_lnk["top"]) / float(native_h)) * c_rect["height"]
                            screen_w = (float(b_lnk["width"]) / float(native_w)) * c_rect["width"]
                            screen_h = (float(b_lnk["height"]) / float(native_h)) * c_rect["height"]
                            slide_data["links"].append({
                                "text": b_lnk["text"],
                                "url": b_lnk["url"],
                                "x_pct": (screen_x / vw) * 100.0,
                                "y_pct": (screen_y / vh) * 100.0,
                                "w_pct": (screen_w / vw) * 100.0,
                                "h_pct": (screen_h / vh) * 100.0
                            })
                            existing_urls.add(b_lnk["url"])
                    for b_txt in b_slide.get("texts", []):
                        if b_txt not in slide_data["texts"]:
                            slide_data["texts"].append(b_txt)

                if previous_png is not None and png_bytes == previous_png:
                    break

                previous_png = png_bytes

                slide_img_path = None
                if CONFIG.get("save_canva_slides_png", True):
                    slide_img_path = os.path.join(presentation_folder, f"{t('file_canva_slide_prefix')}_{slide_num:03d}.png")
                    with open(slide_img_path, "wb") as f_img:
                        f_img.write(png_bytes)

                slides_bundle.append({
                    "png": png_bytes,
                    "img_path": slide_img_path,
                    "texts": slide_data.get("texts", []),
                    "links": slide_data.get("links", [])
                })

                md_description += f"## {t('md_canva_slide')} {slide_num}\n"
                if slide_data.get("texts"):
                    md_description += f"**{t('md_canva_slide_text')}:**\n"
                    for txt in slide_data["texts"]:
                        md_description += f"- {txt}\n"
                if slide_data.get("links"):
                    md_description += f"\n**{t('md_canva_slide_links')}:**\n"
                    for lnk in slide_data["links"]:
                        link_entry = f"- [{lnk['text']}]({lnk['url']})"
                        md_description += link_entry + "\n"
                        if link_entry not in all_sources:
                            all_sources.append(link_entry)
                md_description += "\n---\n\n"

                if page_info and page_info.get("total"):
                    print(t("canva_slide_downloaded_total", current=page_info.get("current", slide_num), total=page_info.get("total")))
                    if page_info.get("current") >= page_info.get("total"):
                        break
                else:
                    print(t("canva_slide_downloaded", current=slide_num))

                if not has_next:
                    break

                clicked_next = pres_page.evaluate('''() => {
                    let nextBtn = document.querySelector('button[aria-label="Next page"], button[aria-label*="Next"], button[aria-label*="Następn"], button[aria-label*="Dalej"], [role="button"][aria-label*="Next"]');
                    if (nextBtn && !nextBtn.disabled && nextBtn.getAttribute('aria-disabled') !== 'true') {
                        nextBtn.click();
                        return true;
                    }
                    return false;
                }''')
                if not clicked_next:
                    pres_page.keyboard.press("ArrowRight")

                time.sleep(0.7)

            if all_sources:
                summary_block = f"## {t('md_canva_all_sources')}\n" + "\n".join(all_sources) + "\n\n---\n\n"
                md_description = md_description.replace("\n\n", "\n\n" + summary_block, 1)

            with open(md_path, "w", encoding="utf-8") as f_md:
                f_md.write(md_description)

            if CONFIG.get("save_canva_as_pdf", True) and slides_bundle:
                print(t("canva_building_pdf", count=len(slides_bundle)))
                if save_slides_as_pdf(slides_bundle, pdf_path, playwright_instance, presentation_folder, native_w, native_h):
                    print(t("canva_pdf_saved", filename=os.path.basename(pdf_path)))

            if tracker:
                tracker.set_file_status(full_url, canva_url, "[X]")

        except Exception as e:
            print(t("canva_error", url=canva_url, error=e))
            sys.stdout.log_error("Canva Presentation", canva_url, str(e))
            all_ok = False
            if tracker:
                tracker.set_file_status(full_url, canva_url, "[!]")
        finally:
            if pres_page:
                try:
                    pres_page.close()
                except Exception:
                    pass

    return all_ok

def extract_structured_sidebar(page):
    structured_links = []
    try:
        page.wait_for_selector("aside.standard-layout-v2__sidebar, [data-circle='app-sidebar'], [data-testid='circle-ai-shell-sidebar-root']", timeout=10000)
        
        sections_v3 = page.locator('.group\\/collapsible-section').all()
        if sections_v3:
            for section in sections_v3:
                section_id = section.get_attribute("id") or ""
                if section_id == "links-section":
                    continue
                    
                section_title = t("default_unknown_section")
                header_el = section.locator('button[data-collapse-trigger="true"] span.truncate, h3[data-testid="spaces-left-sidebar"]')
                if header_el.count() > 0:
                    section_title = header_el.first.inner_text().strip()

                tabs = section.locator('a[href]').all()
                for tab in tabs:
                    href = tab.get_attribute("href")
                    target = tab.get_attribute("target")
                    if href and "/c/" in href and target != "_blank":
                        tab_title = t("default_no_name")
                        name_el = tab.locator('[data-testid="space-name"]')
                        if name_el.count() > 0:
                            tab_title = name_el.first.inner_text().strip()
                        structured_links.append({
                            "url": href,
                            "section": sanitize_name(section_title, CONFIG.get("max_category_name_length", 120)),
                            "tab": sanitize_name(tab_title, CONFIG.get("max_category_name_length", 120))
                        })
            if structured_links:
                return structured_links

        sections = page.locator('aside.standard-layout-v2__sidebar .group.relative[id]').all()
        if not sections:
            return None
            
        for section in sections:
            section_title = t("default_unknown_section")
            if section.locator('h3[data-testid="spaces-left-sidebar"]').count() > 0:
                section_title = section.locator('h3[data-testid="spaces-left-sidebar"]').first.inner_text().strip()
                
            tabs = section.locator('a[href]').all()
            for tab in tabs:
                href = tab.get_attribute("href")
                target = tab.get_attribute("target")
                is_external = tab.evaluate("node => node.closest('#links-section') !== null")
                
                if href and "/c/" in href and target != "_blank" and not is_external:
                    tab_title = t("default_no_name")
                    if tab.locator('h4[data-testid="space-name"]').count() > 0:
                        tab_title = tab.locator('h4[data-testid="space-name"]').first.inner_text().strip()
                        
                    structured_links.append({
                        "url": href,
                        "section": sanitize_name(section_title, CONFIG.get("max_category_name_length", 120)),
                        "tab": sanitize_name(tab_title, CONFIG.get("max_category_name_length", 120))
                    })
    except Exception as e:
        print(t("sidebar_error", error=e))
        return None
        
    return structured_links

def expand_course_sections(page):
    try:
        collapsed_btns = page.locator('[data-testid="summary-course-content"] button[aria-expanded="false"]')
        for i in range(collapsed_btns.count()):
            btn = collapsed_btns.nth(i)
            if btn.is_visible():
                try:
                    btn.click(timeout=2000)
                    time.sleep(0.3)
                except Exception:
                    pass
    except Exception:
        pass

def extract_lessons_from_page(page):
    expand_course_sections(page)
    
    extracted_data = page.evaluate('''() => {
        let results = [];
        let seen = new Set();
        let selectors = [
            '[data-testid="summary-course-content"] a[href*="/lessons/"]',
            'a[href*="/sections/"][href*="/lessons/"]',
            'main .post--card__header a',
            'main [data-testid="post-title"] a',
            'main a:has([data-testid="lesson-content-tittle"])',
            '[data-circle="post-title"] a',
            '[data-testid="post-title"] a'
        ];
        document.querySelectorAll(selectors.join(', ')).forEach(el => {
            let href = el.getAttribute('href');
            if (!href || seen.has(href)) return;
            seen.add(href);
            
            let moduleName = null;
            let groupBox = el.closest('[data-orientation="vertical"][data-index]');
            if (groupBox) {
                let h6 = groupBox.querySelector('h3 h6, button h6');
                if (h6) moduleName = h6.innerText.trim();
            }
            
            let img = el.querySelector('img');
            let thumb = img ? img.getAttribute('src') : null;
            
            results.push({href: href, module: moduleName, thumbnail: thumb});
        });
        return results;
    }''')
    return extracted_data

def get_sidebar_links(page):
    links = []
    try:
        page.wait_for_selector("aside.standard-layout-v2__sidebar, [data-circle='app-sidebar'], [data-testid='circle-ai-shell-sidebar-root']", timeout=10000)
        elements = page.locator("aside.standard-layout-v2__sidebar a, [data-circle='app-sidebar'] a").all()
        for el in elements:
            href = el.get_attribute("href")
            target = el.get_attribute("target")
            is_external = el.evaluate("node => node.closest('#links-section') !== null")
            if href and "/c/" in href and target != "_blank" and not is_external:
                links.append(href)
    except Exception:
        pass
    return list(dict.fromkeys(links))

def ensure_authenticated(page, browser, playwright_context, start_url):
    try:
        page.wait_for_selector('aside.standard-layout-v2__sidebar, [data-circle="app-sidebar"], [data-testid="circle-ai-shell-sidebar-root"], input[type="email"], iframe[src*="challenges.cloudflare.com"], #turnstile-wrapper, main, .post--card, h1, [data-circle="post"]', timeout=15000)
    except Exception:
        pass
        
    needs_action = False
    if "sign_in" in page.url or "login" in page.url or page.locator('input[type="email"]').count() > 0:
        needs_action = True
    elif page.locator('iframe[src*="challenges.cloudflare.com"], #turnstile-wrapper, .cf-turnstile, [name="cf-turnstile-response"]').count() > 0:
        needs_action = True
    elif page.locator('aside.standard-layout-v2__sidebar, [data-circle="app-sidebar"], [data-testid="circle-ai-shell-sidebar-root"]').count() == 0 and page.locator('main, .post--card, h1, [data-circle="post"]').count() == 0:
        needs_action = True

    if needs_action:
        print(t("auth_required"))
        if CONFIG["headless"]:
            print(t("launching_visible_browser"))
            browser.close()
            browser = playwright_context.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            safe_goto(page, start_url)
            
        print(t("manual_login_prompt_1"))
        print(t("manual_login_prompt_2"))
        sys.__stdin__.readline()
        
        page.context.storage_state(path=PLAYWRIGHT_STATE)
        
        if CONFIG["headless"]:
            browser.close()
            browser = playwright_context.chromium.launch(headless=True)
            context = browser.new_context(storage_state=PLAYWRIGHT_STATE, accept_downloads=True)
            page = context.new_page()
            safe_goto(page, start_url)
            
    return browser, page.context, page

def assign_category(lesson_url, sidebar_tree, default_section, default_tab):
    if not sidebar_tree:
        return default_section, default_tab
        
    path = urlparse(lesson_url).path
    for item in sorted(sidebar_tree, key=lambda x: len(x['url']), reverse=True):
        if item['url'] in path:
            return item['section'], item['tab']
            
    return default_section, default_tab

def remove_temp_video_files(folder, video_prefix):
    for filename in os.listdir(folder):
        if filename.startswith(video_prefix):
            if filename.endswith(".mp4") or filename.endswith(".webm") or filename.endswith(".m4a"):
                try:
                    filepath = os.path.join(folder, filename)
                    os.remove(filepath)
                    print(t("removed_temp_file", filename=filename))
                except Exception:
                    pass

def read_multiline_input(prompt_text):
    first_line = input(prompt_text).strip()
    lines = [first_line]
    try:
        if os.name == 'nt':
            import msvcrt
            time.sleep(0.05)
            while msvcrt.kbhit():
                extra_line = sys.__stdin__.readline()
                if not extra_line:
                    break
                lines.append(extra_line.strip())
        else:
            import select
            time.sleep(0.05)
            while select.select([sys.__stdin__], [], [], 0.0)[0]:
                extra_line = sys.__stdin__.readline()
                if not extra_line:
                    break
                lines.append(extra_line.strip())
    except Exception:
        pass
    return " ".join(filter(None, lines))

def parse_input_targets(raw_input, mode):
    cleaned = raw_input.strip()
    if not cleaned:
        return []

    if mode in ['1', '2']:
        single_path = cleaned.strip('"').strip("'")
        if os.path.isfile(single_path):
            try:
                with open(single_path, 'r', encoding='utf-8') as f:
                    cleaned = f.read()
            except Exception:
                pass

        urls = re.findall(r'https?://[^\s,;"\'<>]+', cleaned)
        if urls:
            return list(dict.fromkeys(urls))

        tokens = [tok.strip().strip('"').strip("'") for tok in re.split(r'[\s,;]+', cleaned) if tok.strip()]
        expanded_urls = []
        for tok in tokens:
            if os.path.isfile(tok):
                try:
                    with open(tok, 'r', encoding='utf-8') as f:
                        file_urls = re.findall(r'https?://[^\s,;"\'<>]+', f.read())
                        expanded_urls.extend(file_urls)
                except Exception:
                    pass
            elif tok.startswith("http"):
                expanded_urls.append(tok)
        return list(dict.fromkeys(expanded_urls))

    elif mode == '3':
        single_path = cleaned.strip('"').strip("'")
        if os.path.isfile(single_path):
            return [single_path]
        parts = [p.strip().strip('"').strip("'") for p in re.split(r'[,;\n\r]+|\s+(?=[a-zA-Z0-9_.\-/\\]+\.json)', cleaned) if p.strip()]
        return list(dict.fromkeys(parts)) if parts else [single_path]

    return []

def scrape_materials(targets, state, download_mode):
    if isinstance(targets, str):
        targets = parse_input_targets(targets, download_mode)

    if not targets:
        print(t("no_target_provided"))
        return

    all_lessons_structure = []
    
    if download_mode == '3':
        for json_filepath in targets:
            if not os.path.exists(json_filepath):
                print(t("file_not_found", filepath=json_filepath))
                continue
                
            try:
                with open(json_filepath, 'r', encoding='utf-8') as f:
                    loaded_items = json.load(f)
                    for item in loaded_items:
                        if isinstance(item, dict) and "url" in item:
                            if state.is_url_fully_done(item["url"]):
                                state.mark_step_undone(item["url"], "presentations")
                            if not any(el["url"] == item["url"] for el in all_lessons_structure):
                                all_lessons_structure.append(item)
            except Exception as e:
                print(t("json_load_error", error=e))
                
        if not all_lessons_structure:
            print(t("json_empty"))
            return
            
        parsed_first = urlparse(all_lessons_structure[0]["url"])
        base_url = f"{parsed_first.scheme or 'https'}://{parsed_first.netloc}"
        print(t("loaded_failed_items", count=len(all_lessons_structure)))
    else:
        base_url = targets[0]
        if len(targets) > 1:
            print(t("detected_multiple_targets", count=len(targets)))

    parsed_base = urlparse(base_url)
    platform_domain = parsed_base.netloc
    platform_origin = f"{parsed_base.scheme or 'https'}://{platform_domain}/"

    with sync_playwright() as p:
        context_opts = {"accept_downloads": True}
        if os.path.exists(PLAYWRIGHT_STATE):
            context_opts["storage_state"] = PLAYWRIGHT_STATE
            
        browser = p.chromium.launch(headless=CONFIG["headless"])
        context = browser.new_context(**context_opts)
        page = context.new_page()

        print(t("loading_platform", url=base_url))
        safe_goto(page, base_url)

        browser, context, page = ensure_authenticated(page, browser, p, base_url)
        
        context.storage_state(path=PLAYWRIGHT_STATE)
        cookies_dict = {c['name']: c['value'] for c in context.cookies()}
        export_cookies_to_netscape(context.cookies())
        
        actual_username = page.evaluate('''() => {
            if (window.circleUser && window.circleUser.name) return window.circleUser.name;
            let composerAvatar = document.querySelector('[data-circle="comment-composer"] [role="img"][aria-label]');
            if (composerAvatar) return composerAvatar.getAttribute('aria-label');
            return null;
        }''')
        if not actual_username:
            actual_username = CONFIG.get("fallback_username", "Me")

        captured_vtts = set()
        def on_response(response):
            try:
                if response.url and ".vtt" in response.url.lower():
                    captured_vtts.add(response.url)
            except Exception:
                pass
        page.on("response", on_response)

        sidebar_tree = None
        if download_mode in ['1', '2'] and CONFIG["preserve_sidebar_section_structure"]:
            print(t("reading_sidebar"))
            sidebar_tree = extract_structured_sidebar(page)

        list_items_selector = (
            '[data-testid="summary-course-content"] a[href*="/lessons/"], '
            'a[href*="/sections/"][href*="/lessons/"], '
            '.post--card__header a, '
            '[data-testid="post-title"] a, '
            'a:has([data-testid="lesson-content-tittle"]), '
            '[data-circle="post-title"] a'
        )

        if download_mode == '1':
            for target_url in targets:
                parsed_target = urlparse(target_url)
                target_origin = f"{parsed_target.scheme or 'https'}://{parsed_target.netloc}/"
                path_parts = [part for part in parsed_target.path.split('/') if part]
                is_single_post = ("/lessons/" in target_url) or (len(path_parts) >= 3 and path_parts[0] == 'c' and "sections" not in path_parts)
                
                if is_single_post:
                    section, tab = assign_category(target_url, sidebar_tree, t("default_downloaded_lessons_section"), t("default_lessons_tab"))
                    if not any(el['url'] == target_url for el in all_lessons_structure):
                        all_lessons_structure.append({
                            "url": target_url, 
                            "section": section, 
                            "tab": tab
                        })
                    print(t("single_post_detected", url=target_url))
                else:
                    safe_goto(page, target_url)
                    time.sleep(2)
                    
                    scroll_to_bottom(page, list_items_selector, t("items_posts"), is_comments=False)
                    extracted = extract_lessons_from_page(page)
                    if not extracted: 
                        extracted = [{"href": parsed_target.path, "module": None, "thumbnail": None}]
                    
                    try:
                        fallback_tab = t("default_tab_name")
                        if page.locator('h1').count() > 0:
                            fallback_tab = page.locator('h1').first.inner_text(timeout=3000)
                    except Exception:
                        fallback_tab = t("default_tab_name")
                        
                    fallback_tab = sanitize_name(fallback_tab, CONFIG.get("max_category_name_length", 120))
                    fallback_tab = re.sub(r'^(Back to|Wr[óo]c do)\s+', '', fallback_tab, flags=re.IGNORECASE).strip()

                    for idx_el, lesson_info in enumerate(extracted, start=1):
                        lesson_href = lesson_info["href"]
                        full_lesson_url = urljoin(target_origin, lesson_href)
                        section, tab = assign_category(full_lesson_url, sidebar_tree, t("default_courses_section"), fallback_tab)
                        
                        if not any(el['url'] == full_lesson_url for el in all_lessons_structure):
                            all_lessons_structure.append({
                                "url": full_lesson_url,
                                "section": section,
                                "tab": tab,
                                "module": lesson_info.get("module"),
                                "thumbnail": lesson_info.get("thumbnail"),
                                "course_index": idx_el
                            })

        elif download_mode == '2':
            sidebar_links = get_sidebar_links(page)
            visited_categories = set()

            for target_url in targets:
                parsed_target = urlparse(target_url)
                target_origin = f"{parsed_target.scheme or 'https'}://{parsed_target.netloc}/"
                start_path = parsed_target.path
                
                start_index = 0
                try:
                    start_index = next(i for i, v in enumerate(sidebar_links) if v in start_path)
                except StopIteration:
                    pass
                
                category_queue_urls = sidebar_links[start_index:] if sidebar_links else [start_path]
                
                for cat_url in category_queue_urls:
                    full_cat_url = urljoin(target_origin, cat_url)
                    if full_cat_url in visited_categories or state.is_url_fully_done(full_cat_url):
                        continue
                    visited_categories.add(full_cat_url)
                        
                    section, tab = assign_category(full_cat_url, sidebar_tree, t("default_batch_section"), sanitize_name(os.path.basename(cat_url.strip('/')), CONFIG.get("max_category_name_length", 120)))

                    print(t("scanning_tab", section=section, tab=tab))
                    safe_goto(page, full_cat_url)
                    time.sleep(2)
                    
                    scroll_to_bottom(page, list_items_selector, t("items_posts"), is_comments=False)
                    extracted = extract_lessons_from_page(page)
                    if not extracted:
                        extracted = [{"href": cat_url, "module": None, "thumbnail": None}]
                        
                    for idx_el, lesson_info in enumerate(extracted, start=1):
                        lesson_href = lesson_info["href"]
                        full_lesson_url = urljoin(target_origin, lesson_href)
                        
                        if not any(el['url'] == full_lesson_url for el in all_lessons_structure):
                            all_lessons_structure.append({
                                "url": full_lesson_url,
                                "section": section,
                                "tab": tab,
                                "module": lesson_info.get("module"),
                                "thumbnail": lesson_info.get("thumbnail"),
                                "course_index": idx_el
                            })

        if download_mode in ['1', '2']:
            print(t("extracted_items_count", count=len(all_lessons_structure)))

        tracker = None
        if CONFIG['create_link_status']:
            main_label = base_url if len(targets) == 1 else f"{base_url} (+{len(targets) - 1})"
            tracker = StatusTracker(os.path.join(os.getcwd(), f"{t('file_status_prefix')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"), main_label)

        failed_items = []

        for idx, item in enumerate(all_lessons_structure, start=1):
            if ABORT_FLAG.is_set():
                break
                
            full_url = item["url"]
            parsed_item_url = urlparse(full_url)
            platform_domain = parsed_item_url.netloc or platform_domain
            platform_origin = f"{parsed_item_url.scheme or 'https'}://{platform_domain}/"

            main_section = item.get("section") or item.get("sekcja") or t("default_courses_section")
            category_tab = item.get("tab") or item.get("zakladka") or t("default_tab_name")
            
            if tracker:
                tracker.add_lesson(full_url)
            
            if state.is_url_fully_done(full_url):
                print(t("skipping_completed", idx=idx, total=len(all_lessons_structure), url=full_url))
                if tracker:
                    tracker.set_lesson_status(full_url, "[X]")
                continue

            captured_vtts.clear()
            state.init_url(full_url)
            safe_goto(page, full_url, css_verifier='[data-testid="post-title"], [data-testid="lesson-content-tittle"], [data-circle="post-title"], h2, h1, .post-body, [data-testid="tip-tap-editor-content"], .ProseMirror, [data-circle="rich-text-content"]')
            time.sleep(2)
            expand_full_content(page)
            
            try:
                btn_trans = page.locator('button:has-text("Show transcript"), button:has-text("Pokaż transkrypt")')
                for idx_btn in range(btn_trans.count()):
                    if btn_trans.nth(idx_btn).is_visible():
                        btn_trans.nth(idx_btn).click(position={"x": 5, "y": 5}, timeout=2000)
                        time.sleep(1.5) 
            except Exception:
                pass
            
            lesson_ok = True
            is_lesson = "/lessons/" in full_url
            
            if is_lesson and main_section in ["Pobrane_Lekcje", "Downloaded_Lessons", t("default_downloaded_lessons_section")]:
                try:
                    header_course = page.locator('header[data-testid="circle-ai-shell-inset-header"] span.text-label-md').first
                    if header_course.count() > 0:
                        course_name = header_course.inner_text(timeout=2000).strip()
                        if course_name and sidebar_tree:
                            for s_item in sidebar_tree:
                                if s_item["tab"].lower() == sanitize_name(course_name, CONFIG.get("max_category_name_length", 120)).lower():
                                    main_section = s_item["section"]
                                    category_tab = s_item["tab"]
                                    break
                        elif course_name:
                            category_tab = sanitize_name(course_name, CONFIG.get("max_category_name_length", 120))
                except Exception:
                    pass

            try:
                if is_lesson:
                    title_locators = ['[data-testid="lesson-content-tittle"]', 'main h2', 'h2']
                else:
                    title_locators = ['[data-circle="post-title"]', '[data-testid="post-title"]', 'main h1', 'h2', 'h1']
                lesson_raw_title = f"{t('default_material_prefix')}_{idx}"
                for loc in title_locators:
                    if page.locator(loc).count() > 0:
                        candidate_title = page.locator(loc).first.inner_text(timeout=3000).strip()
                        if candidate_title and not re.match(r'^(Back to|Wr[óo]c do)\s+', candidate_title, re.IGNORECASE):
                            lesson_raw_title = candidate_title
                            break
            except Exception:
                lesson_raw_title = f"{t('default_material_prefix')}_{idx}"

            clean_title = sanitize_name(lesson_raw_title, CONFIG.get("max_post_name_length", 50))
            
            lesson_module = ""
            should_create_module_subfolder = False
            author, pub_date = t("default_unknown_author"), t("default_no_date")
            
            if is_lesson:
                try:
                    if page.locator('.grid-in-right-sidebar a[href*="/lessons/"], #circle-ai-left-sidebar-body').count() == 0:
                        btn_toc = page.locator('button:has(svg[class*="checklist"])')
                        for b_idx in range(btn_toc.count()):
                            if btn_toc.nth(b_idx).is_visible():
                                btn_toc.nth(b_idx).click(position={"x": 5, "y": 5}, timeout=2000)
                                time.sleep(1.5)
                                break
                except Exception:
                    pass
                
                if CONFIG.get("group_into_modules", True):
                    lesson_module_raw = item.get("module") or item.get("modul")
                    if not lesson_module_raw:
                        try:
                            lesson_module_raw = page.evaluate(r'''() => {
                                let activeV3 = document.querySelector('#circle-ai-left-sidebar-body div.bg-tertiary');
                                if (activeV3) {
                                    let card = activeV3.closest('div.border.border-primary');
                                    if (card) {
                                        let h = card.querySelector('span.text-heading-sm');
                                        if (h) return h.innerText.trim();
                                    }
                                }
                                let activeLink = document.querySelector('.grid-in-right-sidebar a[aria-current="page"], .grid-in-right-sidebar a.active');
                                if (!activeLink) return null;
                                let panel = activeLink.closest('div[id*="panel"], div[class*="space-y-"]');
                                if (!panel) return null;
                                let btn = panel.previousElementSibling;
                                if (btn && btn.tagName === 'BUTTON') {
                                    let titleEl = btn.querySelector('h4, h3, h5, span.text-darkest');
                                    return titleEl ? titleEl.innerText.trim() : btn.innerText.split('\n')[0].trim();
                                }
                                return null;
                            }''')
                        except Exception:
                            pass
                    if lesson_module_raw:
                        lesson_module = sanitize_name(lesson_module_raw, CONFIG.get("max_category_name_length", 120))
                        if lesson_module.lower() != sanitize_name(category_tab, CONFIG.get("max_category_name_length", 120)).lower():
                            should_create_module_subfolder = True
                
                target_index = item.get("course_index") or item.get("indeks_kursu") or idx
                try:
                    dom_index = page.evaluate(r'''(currentPath) => {
                        let sidebarV3Items = Array.from(document.querySelectorAll('#circle-ai-left-sidebar-body div.border.border-primary div.py-3 > div'));
                        if (sidebarV3Items.length > 0) {
                            let activeIdx = sidebarV3Items.findIndex(el => el.classList.contains('bg-tertiary') || el.querySelector('.bg-tertiary'));
                            if (activeIdx !== -1) return activeIdx + 1;
                        }
                        let spans = Array.from(document.querySelectorAll('span'));
                        let found = spans.find(s => s.innerText && s.innerText.match(/(Lesson|Lekcja)\s+\d+\s+(of|z)/i));
                        if (found) {
                            let m = found.innerText.match(/(?:Lesson|Lekcja)\s+(\d+)\s+(?:of|z)/i);
                            if (m) return parseInt(m[1]);
                        }
                        let links = Array.from(document.querySelectorAll('.grid-in-right-sidebar a[href*="/lessons/"], [data-testid="summary-course-content"] a[href*="/lessons/"]'));
                        let paths = [];
                        links.forEach(a => {
                            let h = a.getAttribute('href');
                            if (h) {
                                try {
                                    let p = new URL(h, window.location.origin).pathname;
                                    if (!paths.includes(p)) paths.push(p);
                                } catch(e) {}
                            }
                        });
                        let idx = paths.indexOf(currentPath);
                        if (idx !== -1) return idx + 1;
                        return null;
                    }''', urlparse(full_url).path)
                    if dom_index:
                        target_index = int(dom_index)
                except Exception:
                    pass
                
                prefixed_name = f"{target_index:02d}_{clean_title}"
            else:
                try:
                    page.wait_for_selector('[data-testid="post-meta-info-regular"], .post__header, .post__user, [data-circle="post-author"]', timeout=5000)
                except Exception:
                    pass
                    
                try:
                    date_data = page.evaluate(r'''() => {
                        let author = "Unknown";
                        let pubDate = "No date";
                        
                        let authorNodeV3 = document.querySelector('[data-circle="post-author-name"]');
                        if (authorNodeV3) author = authorNodeV3.innerText.trim();
                        
                        let timeNodeV3 = document.querySelector('[data-circle="post-author-timestamp"]');
                        if (timeNodeV3) {
                            pubDate = timeNodeV3.getAttribute('datetime') || timeNodeV3.getAttribute('title') || timeNodeV3.innerText.trim();
                        }
                        
                        if (authorNodeV3 || timeNodeV3) return { author: author, pub_date: pubDate };
                        
                        let header = document.querySelector('[data-testid="post-meta-info-regular"]') || document.querySelector('.post__header') || document.querySelector('.post__user');
                        if (!header) return { author: author, pub_date: pubDate };
                        
                        let authorEl = header.querySelector('.author__name a') || header.querySelector('.author__name');
                        if (authorEl) author = authorEl.innerText.trim();
                        
                        let timeEl = header.querySelector('time');
                        if (timeEl && timeEl.getAttribute('datetime')) {
                            return { author: author, pub_date: timeEl.getAttribute('datetime').substring(0, 10) };
                        }
                        
                        let dateNode = header.querySelector('.author__time span') || header.querySelector('.ago') || header.querySelector('.author__time');
                        if (dateNode) pubDate = dateNode.innerText.trim();
                        
                        return { author: author, pub_date: pubDate };
                    }''')
                    author = date_data.get("author", "Unknown")
                    if author == "Unknown":
                        author = t("default_unknown_author")
                    pub_date = date_data.get("pub_date", "No date")
                except Exception:
                    pass

                formatted_date = parse_date(pub_date)
                prefixed_name = f"{formatted_date}_{clean_title}"

            if any(ignored.lower() in clean_title.lower() for ignored in CONFIG["ignored_titles"]):
                print(t("skipping_excluded", title=clean_title))
                state.mark_all_done(full_url)
                if tracker:
                    tracker.set_lesson_status(full_url, t("status_skipped"))
                continue

            print(f"\n==================================================")
            module_text = f" -> {lesson_module}" if should_create_module_subfolder else ""
            print(t("processing_item", idx=idx, total=len(all_lessons_structure), name=prefixed_name, section=main_section, tab=category_tab, module_text=module_text))

            try:
                folder_path_elements = []
                if CONFIG["preserve_sidebar_section_structure"]:
                    folder_path_elements.append(main_section)
                    folder_path_elements.append(category_tab)
                    if should_create_module_subfolder:
                        folder_path_elements.append(lesson_module)
                else:
                    main_folder = sanitize_name(category_tab, CONFIG.get("max_category_name_length", 120))
                    folder_path_elements.append(main_folder)
                    if should_create_module_subfolder:
                        folder_path_elements.append(lesson_module)
                        
                folder_path_elements.append(prefixed_name)
                
                target_folder = build_compatible_path(os.getcwd(), folder_path_elements, is_file=False)
                os.makedirs(target_folder, exist_ok=True)
                
                final_post_name = os.path.basename(target_folder)
                file_prefix = f"{final_post_name}_"
                
                media_dump_content = f"# Media Dump: {clean_title}\n**{t('md_original_link')}:** {full_url}\n\n"

                if not state.is_step_done(full_url, "content"):
                    content_ok = True
                    content_text = t("default_no_description")
                    try:
                        extracted_content = page.evaluate(r'''(embeddedLabel) => {
                            let container = document.querySelector('[data-testid="post-body-inside"], [data-circle="post-body"], main [data-testid="tip-tap-editor-content"], .post-body, .ProseMirror');
                            if (!container) return "";
                            
                            let clone = container.cloneNode(true);
                            
                            clone.querySelectorAll('iframe, video, hls-video').forEach(media => {
                                let src = media.getAttribute('src');
                                if (src) {
                                    if (src.startsWith('//')) src = 'https:' + src;
                                    let targetLink = src;
                                    if (src.includes('iframe.ly')) {
                                        try {
                                            let urlObj = new URL(src);
                                            let hiddenUrl = urlObj.searchParams.get('url');
                                            if (hiddenUrl) targetLink = decodeURIComponent(hiddenUrl);
                                        } catch(e) {}
                                    }
                                    const textNode = document.createElement('span');
                                    textNode.innerText = '\n[' + embeddedLabel + ': ' + targetLink + ']\n';
                                    media.parentNode.insertBefore(textNode, media);
                                }
                            });
                            
                            clone.querySelectorAll('a[href]').forEach(a => {
                                let href = a.getAttribute('href');
                                if (href && !href.startsWith('javascript:') && !href.startsWith('mailto:')) {
                                    if (a.innerText && !a.innerText.includes(href)) {
                                        a.innerText = a.innerText + ' (' + href + ')';
                                    }
                                }
                            });
                            
                            clone.querySelectorAll('button').forEach(btn => {
                                let txt = (btn.innerText || btn.textContent || "").toLowerCase();
                                if (txt.includes('show transcript') || txt.includes('hide transcript') || txt.includes('pokaż transkrypt') || txt.includes('see more')) {
                                    btn.remove();
                                }
                            });
                            
                            return clone.innerText.trim();
                        }''', t("md_embedded_media"))
                        if extracted_content:
                            content_text = extracted_content
                        
                        saved_author = author if not is_lesson else t("default_course_author")
                        saved_date = formatted_date if not is_lesson else t("default_no_date")
                        
                        content_file = build_compatible_path(target_folder, [f"{file_prefix}{t('file_content_suffix')}"], is_file=True)
                        with open(content_file, "w", encoding="utf-8") as f:
                            f.write(f"# {clean_title}\n**{t('md_original_link')}:** {full_url}\n**{t('md_author')}:** {saved_author} | **{t('md_publish_date')}:** {saved_date}\n\n## {t('md_content_header')}:\n{content_text}\n")
                        print(t("saved_main_content"))
                    except Exception as e:
                        print(t("content_fetch_error", error=e))
                        content_ok = False
                        lesson_ok = False
                        
                    try:
                        links_in_content = page.locator('[data-testid="post-body-inside"] a[href], [data-circle="post-body"] a[href], main [data-testid="tip-tap-editor-content"] a[href]').all()
                        saved_links = set()
                        for a_tag in links_in_content:
                            if a_tag.evaluate("el => el.closest('[data-circle=\"comment\"], .post--comment, [data-testid=\"courses-v2-comments\"]') !== null"):
                                continue
                            href = a_tag.get_attribute("href")
                            link_text = a_tag.inner_text().strip() or href
                            if href and href not in saved_links and not href.startswith(('javascript:', 'mailto:')):
                                full_href = urljoin(platform_origin, href)
                                saved_links.add(href)
                                media_dump_content += f"- {t('md_link_from_content')}: [{link_text}]({full_href})\n"
                    except Exception as e:
                        print(t("content_links_error", error=e))
                        
                    if content_ok:
                        state.mark_step_done(full_url, "content")

                if not state.is_step_done(full_url, "presentations"):
                    pres_ok = True
                    if CONFIG.get("download_canva_presentations", True):
                        if not download_canva_presentations(page, context, p, target_folder, file_prefix, full_url, tracker):
                            pres_ok = False
                            lesson_ok = False
                    if pres_ok:
                        state.mark_step_done(full_url, "presentations")

                if not state.is_step_done(full_url, "comments") and (CONFIG["save_comments_md"] or CONFIG["save_comments_json"]):
                    switch_tab(page, ["Comments", "Komentarze"])
                    
                    try:
                        if page.locator('.post--comment, [data-testid="message-item"], article[data-circle="comment"], div[data-circle="comment"]').count() == 0:
                            btn_kom = page.locator('button[data-testid="comment-lesson"], button:has(svg[class*="comment"]), button[aria-label*="Comments"]')
                            for idx_btn in range(btn_kom.count()):
                                if btn_kom.nth(idx_btn).is_visible():
                                    btn_kom.nth(idx_btn).click(position={"x": 5, "y": 5})
                                    time.sleep(2)
                                    break
                    except Exception:
                        pass

                    comments_list = []
                    
                    scroll_to_bottom(page, '.post--comment, [data-testid="message-item"], article[data-circle="comment"], div[data-circle="comment"]', t("items_comments"), is_comments=True)
                    comments_list.extend(scrape_visible_comments(page, actual_username))
                    
                    try:
                        thread_count = page.locator('button[data-testid="replies-block"]').count()
                        if thread_count > 0:
                            for i in range(thread_count):
                                btn_thread = page.locator('button[data-testid="replies-block"]').nth(i)
                                if btn_thread.is_visible():
                                    try:
                                        btn_thread.click(timeout=3000)
                                        time.sleep(2)
                                        scroll_to_bottom(page, '[data-testid="message-item"], article[data-circle="comment"]', t("items_replies_in_thread", idx=i+1), is_comments=True)
                                        comments_list.extend(scrape_visible_comments(page, actual_username))
                                        
                                        btn_back = page.locator('section#rail-content button[aria-label="Back"], button[aria-label="Back"]').first
                                        if btn_back.count() > 0 and btn_back.is_visible():
                                            btn_back.click(timeout=3000)
                                            time.sleep(1.5)
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                    json_file = build_compatible_path(target_folder, [f"{file_prefix}{t('file_comments_json_suffix')}"], is_file=True)
                    existing_comments = []
                    
                    if os.path.exists(json_file):
                        try:
                            with open(json_file, 'r', encoding='utf-8') as f:
                                existing_comments = json.load(f)
                        except Exception:
                            pass
                            
                    def get_comment_key(c):
                        c_author = c.get('author') or c.get('autor') or ''
                        c_date = c.get('date') or c.get('data') or ''
                        c_content = c.get('content') or c.get('tresc') or ''
                        return f"{c_author}_{c_date}_{c_content}"

                    existing_keys = [get_comment_key(c) for c in existing_comments]
                    added_new = 0
                    
                    for nc in comments_list:
                        key = get_comment_key(nc)
                        if key not in existing_keys:
                            existing_comments.append(nc)
                            existing_keys.append(key)
                            added_new += 1

                    if existing_comments:
                        if CONFIG["save_comments_json"]:
                            with open(json_file, "w", encoding="utf-8") as f:
                                json.dump(existing_comments, f, indent=4, ensure_ascii=False)
                        
                        if CONFIG["save_comments_md"]:
                            md_content = f"# {t('md_comments_header')}: {clean_title}\n\n"
                            for c in existing_comments:
                                c_author = c.get('author') or c.get('autor') or t("default_unknown_author")
                                c_date = c.get('date') or c.get('data') or ''
                                c_body = c.get('content') or c.get('tresc') or ''
                                date_str = f" ({c_date})" if c_date and c_date not in ["Brak daty", "No date", t("default_no_date")] else ""
                                md_content += f"**{c_author}**{date_str}:\n> {c_body}\n\n---\n"
                            comments_md_file = build_compatible_path(target_folder, [f"{file_prefix}{t('file_comments_md_suffix')}"], is_file=True)
                            with open(comments_md_file, "w", encoding="utf-8") as f:
                                f.write(md_content)
                                
                        if added_new > 0 or len(existing_comments) > 0:
                            print(t("saved_comments", total=len(existing_comments), new=added_new))
                            
                    state.mark_step_done(full_url, "comments")
                elif not (CONFIG["save_comments_md"] or CONFIG["save_comments_json"]):
                    state.mark_step_done(full_url, "comments")

                if not state.is_step_done(full_url, "attachments") and CONFIG["download_attachments"]:
                    att_ok = True
                    switch_tab(page, ["Files", "Pliki"])
                    files_to_download = []
                    
                    for j, img in enumerate(page.locator('[data-testid="post-body-inside"] img, [data-circle="post-body"] img, main [data-testid="tip-tap-editor-content"] img').all()):
                        if img.evaluate("el => el.closest('[data-circle=\"comment\"], .post--comment, [data-testid=\"courses-v2-comments\"], .node-embed') !== null"):
                            continue
                        src = img.get_attribute('src')
                        if src and not src.startswith('data:'):
                            full_src = urljoin(platform_origin, src)
                            img_name = sanitize_name(unquote(os.path.basename(urlparse(full_src).path)), CONFIG.get("max_post_name_length", 50))
                            if len(img_name) > 50 or not img_name or img_name in ["Brak_nazwy", "Unnamed", t("default_no_name")]:
                                img_name = f"{t('file_image_prefix')}_{j}"
                            if img_name.lower().endswith('.pdf'):
                                img_name = img_name[:-4] + "_preview.jpg"
                            attachment_path = build_compatible_path(target_folder, [f"{file_prefix}{img_name}"], is_file=True)
                            files_to_download.append((full_src, attachment_path))

                    try:
                        attachments_selector = 'a:has(svg[class*="course-file"]), .node-file a[download], a[data-testid="post-attachment"], a[href*="assets-v2.circle.so"], a[href*="circle.so/rails/active_storage"], div[role="row"] a[download]'
                        if page.locator(attachments_selector).count() == 0:
                            btn_files = page.locator('button:has(svg[class*="course-files"]), button[aria-label*="Files"], button[aria-label*="Pliki"]')
                            for idx_btn in range(btn_files.count()):
                                if btn_files.nth(idx_btn).is_visible():
                                    btn_files.nth(idx_btn).click(position={"x": 5, "y": 5})
                                    time.sleep(2)
                                    break
                    except Exception:
                        pass

                    for att in page.locator(attachments_selector).all():
                        href = att.get_attribute('href')
                        if href:
                            file_url = urljoin(platform_origin, href)
                            
                            text_name = ""
                            if att.locator('span.truncate').count() > 0:
                                text_name = att.locator('span.truncate').first.inner_text().strip()
                            else:
                                text_name = att.inner_text().strip()
                                text_name = re.split(r'\n|\r', text_name)[0].strip()
                                
                            suggested_name = sanitize_name(unquote(text_name or os.path.basename(urlparse(file_url).path)), CONFIG.get("max_post_name_length", 50))
                            if not suggested_name or suggested_name in ["Brak_nazwy", "Unnamed", t("default_no_name")] or len(suggested_name) > 50:
                                suggested_name = sanitize_name(unquote(os.path.basename(urlparse(file_url).path)), CONFIG.get("max_post_name_length", 50))
                                
                            if tracker:
                                tracker.add_file(full_url, file_url, t("md_attachment"))
                            media_dump_content += f"- {t('md_attachment')}: {file_url}\n"
                            attachment_path = build_compatible_path(target_folder, [f"{file_prefix}{suggested_name}"], is_file=True)
                            files_to_download.append((file_url, attachment_path))

                    if CONFIG.get("download_lesson_thumbnails", False):
                        try:
                            img_src = item.get("thumbnail") or item.get("miniaturka")
                            if not img_src:
                                thumbnail_loc = page.locator('#circle-ai-left-sidebar-body div.bg-tertiary img, a[aria-current="page"] img, .group\\/sidebar-item.active img').first
                                if thumbnail_loc.count() > 0:
                                    img_src = thumbnail_loc.get_attribute('src')
                            if img_src and "placeholder" not in img_src.lower():
                                full_img_src = urljoin(platform_origin, img_src)
                                thumb_name = sanitize_name(t("file_thumbnail_name"), CONFIG.get("max_post_name_length", 50))
                                thumb_path = build_compatible_path(target_folder, [f"{file_prefix}{thumb_name}"], is_file=True)
                                files_to_download.append((full_img_src, thumb_path))
                        except Exception:
                            pass

                    unique_links = {}
                    for u, pth in files_to_download:
                        if u not in unique_links:
                            unique_links[u] = pth
                    files_to_download = list(unique_links.items())

                    if files_to_download:
                        print(t("downloading_files_count", count=len(files_to_download)))
                        with ThreadPoolExecutor(max_workers=CONFIG["max_concurrent_downloads"]) as executor:
                            futures = {executor.submit(download_file_with_resume, lnk, pth, cookies_dict, platform_origin): lnk for lnk, pth in files_to_download}
                            for future in as_completed(futures):
                                lnk = futures[future]
                                res = future.result()
                                if not res: 
                                    att_ok = False
                                    lesson_ok = False
                                elif tracker:
                                    tracker.set_file_status(full_url, lnk, "[X]")
                    if att_ok:
                        state.mark_step_done(full_url, "attachments")
                elif not CONFIG["download_attachments"]:
                    state.mark_step_done(full_url, "attachments")

                if not state.is_step_done(full_url, "transcripts"):
                    trans_ok = True
                    transcript_urls = [u for u in captured_vtts if 'mosaic' not in u.lower() and 'thumbnail' not in u.lower()]
                    
                    for i_vtt, u_vtt in enumerate(transcript_urls, start=1):
                        vtt_name = sanitize_name(f"{t('file_transcript_prefix')}_{i_vtt}.vtt")
                        vtt_path = build_compatible_path(target_folder, [f"{file_prefix}{vtt_name}"], is_file=True)
                        if tracker:
                            tracker.add_file(full_url, u_vtt, t("md_transcript"))
                        
                        if os.path.exists(vtt_path):
                            print(t("transcript_complete", filename=os.path.basename(vtt_path)))
                            media_dump_content += f"- {t('md_transcript')}: {u_vtt}\n"
                            if tracker:
                                tracker.set_file_status(full_url, u_vtt, "[X]")
                        else:
                            try:
                                r_vtt = requests.get(u_vtt, cookies=cookies_dict, headers={'Referer': platform_origin}, timeout=15)
                                if r_vtt.status_code in [200, 206]:
                                    with open(vtt_path, "wb") as f_vtt:
                                        f_vtt.write(r_vtt.content)
                                    print(t("transcript_downloaded", filename=os.path.basename(vtt_path)))
                                    media_dump_content += f"- {t('md_transcript')}: {u_vtt}\n"
                                    if tracker:
                                        tracker.set_file_status(full_url, u_vtt, "[X]")
                            except Exception as e:
                                print(t("transcript_error", error=e))
                                sys.stdout.log_error("Transcript", u_vtt, str(e))
                                trans_ok = False
                                lesson_ok = False
                                if tracker:
                                    tracker.set_file_status(full_url, u_vtt, "[!]")
                            
                    if trans_ok:
                        state.mark_step_done(full_url, "transcripts")

                if not state.is_step_done(full_url, "videos"):
                    vid_ok = True
                    try:
                        page.wait_for_selector("iframe, hls-video, video", timeout=3000)
                    except Exception:
                        pass
                        
                    video_urls = []
                    allowed_video_domains = [
                        'youtube.com', 'youtu.be', 'vimeo.com', 'wistia.com',
                        'loom.com', 'cloudflarestream.com', 'circle.so',
                        'cdn-media.circle.so', platform_domain
                    ]

                    def process_media_nodes(container_locator, extract_embeds, extract_links, exclude_comments=False):
                        extracted = []
                        if container_locator.count() == 0:
                            return extracted
                            
                        if extract_embeds:
                            nodes = container_locator.locator("iframe, hls-video, source, video").all()
                            for node in nodes:
                                try:
                                    if exclude_comments and node.evaluate("n => n.closest('[data-circle=\"comment\"], .post--comment, [data-testid=\"courses-v2-comments\"]') !== null"):
                                        continue
                                    src = node.get_attribute("src")
                                    if src and "stripe.com" not in src and "canva.com" not in src and not src.startswith("blob:"):
                                        is_iframe = node.evaluate("n => n.tagName.toLowerCase() === 'iframe'")
                                        if is_iframe:
                                            if not any(d in src.lower() for d in allowed_video_domains):
                                                continue
                                        if src.startswith("//"):
                                            src = "https:" + src
                                        elif src.startswith("/"):
                                            src = urljoin(platform_origin, src)
                                        if src not in extracted:
                                            extracted.append(src)
                                except Exception:
                                    pass
                                    
                        if extract_links:
                            nodes = container_locator.locator("a[href]").all()
                            for node in nodes:
                                try:
                                    if exclude_comments and node.evaluate("n => n.closest('[data-circle=\"comment\"], .post--comment, [data-testid=\"courses-v2-comments\"]') !== null"):
                                        continue
                                    href = node.get_attribute("href")
                                    if href and not href.startswith(('javascript:', 'mailto:')):
                                        if any(d in href.lower() for d in allowed_video_domains) and "/c/" not in href:
                                            if href not in extracted:
                                                extracted.append(href)
                                except Exception:
                                    pass
                        return extracted

                    post_container = page.locator('.post--parent, [data-circle="post"], .featured-media-html, main')
                    post_urls = process_media_nodes(
                        post_container, 
                        CONFIG.get("download_embedded_videos_posts", True), 
                        CONFIG.get("download_linked_videos_posts", False),
                        exclude_comments=True
                    )
                    for u in post_urls:
                        if u not in video_urls:
                            video_urls.append(u)

                    comments_container = page.locator('.post--comment, article[data-circle="comment"], div[data-circle="comment"], [data-testid="courses-v2-comments"]')
                    comment_urls = process_media_nodes(
                        comments_container, 
                        CONFIG.get("download_embedded_videos_comments", True), 
                        CONFIG.get("download_linked_videos_comments", False),
                        exclude_comments=False
                    )
                    for u in comment_urls:
                        if u not in video_urls:
                            video_urls.append(u)
                            
                    if video_urls:
                        for idx_video, video_url in enumerate(video_urls, start=1):
                            media_dump_content += f"- {t('md_video_source')}: {video_url}\n"
                            if tracker:
                                tracker.add_file(full_url, video_url, f"Video {idx_video}")
                            
                            is_external = not any(d in video_url for d in ['cloudflarestream.com', 'circle.so', platform_domain])
                            if is_external and not CONFIG.get("download_external_videos", False):
                                print(t("skipping_external_video", url=video_url))
                                if tracker:
                                    tracker.set_file_status(full_url, video_url, t("status_skipped_external"))
                                continue
                            
                            video_path_check = build_compatible_path(target_folder, [f"{file_prefix}{t('file_video_prefix')}_{idx_video}.mkv"], is_file=True)
                            legacy_video_check = build_compatible_path(target_folder, [f"{file_prefix}wideo_{idx_video}.mkv"], is_file=True)
                            if os.path.exists(video_path_check) or os.path.exists(legacy_video_check):
                                existing_vid = video_path_check if os.path.exists(video_path_check) else legacy_video_check
                                print(t("video_complete", filename=os.path.basename(existing_vid)))
                                if tracker:
                                    tracker.set_file_status(full_url, video_url, "[X]")
                                continue
                            
                            ydl_opts_check = {
                                'quiet': True,
                                'no_warnings': True,
                                'ignoreerrors': True,
                                'cookiefile': COOKIES_FILE,
                                'referer': platform_origin
                            }
                            
                            info = None
                            try:
                                with yt_dlp.YoutubeDL(ydl_opts_check) as ydl:
                                    info = ydl.extract_info(video_url, download=False)
                            except Exception:
                                pass
                                
                            if info:
                                if CONFIG["create_media_dump"]:
                                    for fmt in info.get('requested_formats', []):
                                        media_dump_content += f"  - {t('md_video_resolution')} [{fmt.get('resolution', 'audio/video')}] ({fmt.get('ext')}): {fmt.get('url')}\n"
                                        
                                orig_lang = None
                                try:
                                    if "youtube.com" in video_url or "youtu.be" in video_url:
                                        import urllib.request
                                        subs_info_url = f"https://www.youtube.com/api/timedtext?v={info.get('id')}&type=list"
                                        req = urllib.request.Request(subs_info_url)
                                        req.add_header('Cookie', open(COOKIES_FILE).read().replace('\n', '; '))
                                        response = urllib.request.urlopen(req, timeout=10)
                                        subs_xml = response.read().decode('utf-8')
                                        
                                        m_orig = re.search(r'<track[^>]+lang_code="([^"]+)"[^>]+(?:kind="asr"|lang_default="true")', subs_xml)
                                        orig_lang = m_orig.group(1) if m_orig else None
                                except Exception:
                                    orig_lang = None

                                langs_to_dl = ['pl', 'en']
                                if orig_lang and orig_lang not in langs_to_dl:
                                    langs_to_dl.append(orig_lang)
                                
                                video_path = video_path_check
                                video_prefix_name = f"{file_prefix}{t('file_video_prefix')}_{idx_video}"
                                ydl_opts_download = {
                                    'outtmpl': video_path,
                                    'cookiefile': COOKIES_FILE,
                                    'referer': platform_origin,
                                    'format': 'bestvideo[protocol^=http][height<=1080]+bestaudio/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
                                    'merge_output_format': 'mkv',
                                    'format_sort': [
                                        'res:1080',
                                        'proto',
                                        'vbr:min'
                                    ],
                                    'writesubtitles': True,
                                    'writeautomaticsub': True,
                                    'subtitleslangs': langs_to_dl,
                                    'embedsubtitles': True,
                                    'keepvideo': True,
                                    'postprocessors': [
                                        {'key': 'FFmpegEmbedSubtitle'}
                                    ],
                                    'quiet': False,
                                    'verbose': False,
                                    'no_warnings': False,
                                    'ignoreerrors': True,
                                    'concurrent_fragment_downloads': 16, 
                                    'retries': 999,
                                    'fragment_retries': 999,
                                    'retry_sleep': 10,
                                    'skip_unavailable_fragments': False
                                }
                                
                                print(t("video_downloading", url=video_url))
                                try:
                                    with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
                                        error_code = ydl.download([video_url])
                                    if error_code == 0:
                                        remove_temp_video_files(target_folder, video_prefix_name)
                                        if tracker:
                                            tracker.set_file_status(full_url, video_url, "[X]")
                                    else:
                                        print(t("video_download_problem", code=error_code))
                                        sys.stdout.log_error("Video", video_url, f"yt-dlp error code: {error_code}")
                                        vid_ok = False
                                        lesson_ok = False
                                        if tracker:
                                            tracker.set_file_status(full_url, video_url, t("status_download_error"))
                                except Exception as e:
                                    print(t("video_critical_error", error=e))
                                    sys.stdout.log_error("Video", video_url, str(e))
                                    vid_ok = False
                                    lesson_ok = False
                                    if tracker:
                                        tracker.set_file_status(full_url, video_url, t("status_critical_error"))
                                    
                    if vid_ok:
                        state.mark_step_done(full_url, "videos")

                if CONFIG["create_media_dump"]:
                    media_dump_file = build_compatible_path(target_folder, [f"{file_prefix}{t('file_media_dump_suffix')}"], is_file=True)
                    with open(media_dump_file, "w", encoding="utf-8") as f:
                        f.write(media_dump_content)

                if tracker:
                    tracker.set_lesson_status(full_url, "[X]" if lesson_ok else "[!]")
                    
            except Exception as e:
                print(t("unexpected_lesson_error", error=e))
                sys.stdout.log_error("Lesson/Post", full_url, str(e))
                lesson_ok = False
                dump_filename = f"DEBUG_DUMP_{idx:02d}.html"
                try:
                    with open(dump_filename, "w", encoding="utf-8") as f_dump:
                        f_dump.write(page.content())
                    print(t("html_dump_created", filename=dump_filename))
                except Exception as dump_e:
                    print(t("html_dump_error", error=dump_e))
                
                if tracker:
                    tracker.set_lesson_status(full_url, "[!]")
            
            if not lesson_ok:
                failed_items.append(item)

        context.storage_state(path=PLAYWRIGHT_STATE)
        export_cookies_to_netscape(context.cookies())
        
        print("\n" + "="*50)
        print(t("all_tasks_finished"))
        if sys.stdout.error_ledger:
            print(t("errors_registered_count", count=len(sys.stdout.error_ledger)))
            for err in sys.stdout.error_ledger:
                print(err)
        else:
            print(t("no_errors_registered"))
        print("="*50 + "\n")
        
        if failed_items:
            failed_filename = f"{t('file_failed_prefix')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(failed_filename, "w", encoding="utf-8") as f:
                json.dump(failed_items, f, indent=4, ensure_ascii=False)
            print(t("failed_file_generated", filename=failed_filename))
            print(t("failed_file_hint"))
            print(t("state_kept_on_error"))
        else:
            if not CONFIG.get("keep_state_on_completion", True):
                state.clear_state()
            else:
                print(t("state_kept_on_completion"))
            
        browser.close()

if __name__ == "__main__":
    print(t("menu_title"))
    print(t("menu_opt_1"))
    print(t("menu_opt_2"))
    print(t("menu_opt_3"))
    mode_input = input(t("menu_choice_prompt")).strip()
    if not mode_input:
        mode_input = '1'

    raw_target_input = ""
    if mode_input in ['1', '2']:
        raw_target_input = read_multiline_input(t("prompt_target_url"))
    elif mode_input == '3':
        raw_target_input = read_multiline_input(t("prompt_error_file"))
    
    parsed_targets = parse_input_targets(raw_target_input, mode_input)
    if parsed_targets:
        state_mgr = StateManager()
        restore_state_from_disk(state_mgr)
        try:
            scrape_materials(parsed_targets, state_mgr, mode_input)
        except KeyboardInterrupt:
            ABORT_FLAG.set()
            print(t("keyboard_interrupt"))
            sys.exit(1)
        except Exception as e:
            print(t("critical_main_error", error=e))
    else:
        print(t("no_target_provided"))