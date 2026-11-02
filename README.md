# Circle.so Community & Course Archiver

An automated, fault-tolerant archiver and scraper for **Circle.so** communities and courses (including custom domains), built with Python, Playwright, and `yt-dlp`.

Supports full localization (English `en` and Polish `pl` out of the box via `locales.json`), incremental state tracking, resume-capable downloads, and interactive PDF generation for embedded Canva presentations.

---

## Key Features

* **Multi-Generation Circle.so Compatibility:** Works seamlessly with both legacy **Circle Layout v2** (`standard-layout-v2`) and the newest **Circle AI Shell (v3) / Header v4** workspace architecture.
* **Complete Content Archiving:**
  * Extracts post and lesson rich-text content (`TipTap` / `ProseMirror`) into clean Markdown (`content.md`) with preserved inline links and embedded media references.
  * Scrapes all comments and nested thread replies into `.json` and `.md` files with automatic deduplication across runs.
  * Multi-threaded downloading of attachments, course files, inline images, and lesson thumbnails with HTTP `Range` resume support.
  * Network-level interception and downloading of original `.vtt` video subtitle/transcript files.
* **Interactive Canva Presentation Exporter:**
  * Automatically detects embedded Canva presentations (`[canva.com/design/](https://canva.com/design/)...`).
  * Reads native canvas dimensions and metadata directly from Canva's `window.bootstrap` object.
  * Captures full-resolution slides (`slide_001.png`, ...), extracts slide text and source URLs into Markdown, and compiles an **interactive PDF with clickable hyperlinks** positioned over their exact slide coordinates.
* **Advanced Video Downloading (`yt-dlp`):**
  * Downloads native Circle.so / Cloudflare Stream videos and optional external embeds (YouTube, Vimeo, Loom, Wistia) up to 1080p in `.mkv` format.
  * Automatically fetches and embeds subtitles (`en`, `pl`, and original audio language) using FFmpeg.
* **Crash Recovery & Granular State Management:**
  * Tracks 6 individual steps per URL (`content`, `presentations`, `comments`, `attachments`, `transcripts`, `videos`) in `progress_state.json`.
  * Fast local folder scanner restores completion history on startup and detects incomplete Canva presentation folders.
  * Automatically generates a `failed_items_*.json` file if any item fails, allowing one-click retry via Mode 3.
* **Flexible Input:** Accepts a single URL, multiple URLs separated by spaces, commas, or newlines, or a path to a `.txt` file containing a list of URLs.

---

## Circle.so Version Compatibility

Circle.so is a cloud-hosted SaaS platform deployed continuously (Ruby on Rails backend with ActiveStorage + React/TipTap frontend). While Circle does not expose a single traditional version number, its frontend DOM uses generation markers.

This scraper uses cascading fallback selectors and is compatible with all current Circle.so UI generations:

| Circle UI Generation | DOM Identifiers Supported | Status |
| :--- | :--- | :--- |
| **Circle Layout v2** (Classic 2.0) | `aside.standard-layout-v2__sidebar`, `.post--card`, `[data-testid="courses-v2-comments"]` | Supported |
| **Circle AI Shell v3** (2025-2026) | `[data-testid="circle-ai-shell-sidebar-root"]`, `.group/collapsible-section`, `[data-circle="post"]` | Supported |
| **Circle Header v4** (2026+) | `[data-testid="header-v4-left-slot"]`, `[data-circle="app-topbar"]` | Supported |

### How to verify if your target platform is supported:
1. Open the target community in your browser and press `F12` (Developer Tools).
2. In the **Console** tab, type `window.circleUser` and press Enter. If it returns an object containing your user profile data, the site is powered by Circle.so.
3. In the **Elements** tab, search (`Ctrl+F`) for `circle-ai-shell` (Layout v3) or `standard-layout-v2` (Layout v2).

---

## Requirements

1. **Python 3.9+**
2. **FFmpeg** installed and available in your system `PATH` (required by `yt-dlp` for stream merging and subtitle embedding).
3. Python packages listed in `requirements.txt`.

---

## Installation

1. Clone this repository:
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

2. Install required Python dependencies:
```bash
pip install -r requirements.txt
```

3. Install the Playwright Chromium browser binary:
```bash
playwright install chromium
```

---

## Usage

Run the main script:
```bash
python scrape_all.py
```

### Operating Modes:
* **Mode 1 (Selected Links):** Downloads only the pasted link(s). You can paste a single post/lesson URL, a full course/space tab URL, multiple URLs separated by spaces/commas/newlines, or provide a path to a `.txt` file with URLs.
* **Mode 2 (Sequential Batch):** Starts from the pasted sidebar tab URL and sequentially downloads all lessons/posts from that tab and all subsequent tabs downwards along the sidebar hierarchy.
* **Mode 3 (Retry Failed Items):** Reads a previously generated `failed_items_*.json` file and retries only the incomplete steps.

### Authentication & Cloudflare Verification:
On your first run (or whenever your session expires), the script will automatically launch a visible browser window if login or anti-bot verification (Cloudflare Turnstile) is detected.
1. Log in manually in the opened browser window.
2. Wait until the main platform view loads completely.
3. Press **ENTER** in the terminal. The script will save your session to `playwright_state.json` and `cookies.txt` for future runs.

---

## Configuration (`config.json`)

On the first run, `config.json` is generated automatically. You can customize the behavior using the following parameters:

| Option | Default | Description |
| :--- | :--- | :--- |
| `language` | `"pl"` | Interface and output filename language (`"en"` for English, `"pl"` for Polish, defined in `locales.json`). |
| `download_attachments` | `true` | Download post/lesson file attachments and inline images. |
| `download_canva_presentations` | `true` | Detect and scrape embedded Canva presentations (`[canva.com/design/](https://canva.com/design/)...`). |
| `save_canva_as_pdf` | `true` | Compile captured Canva slides into an interactive PDF with clickable links. |
| `save_canva_slides_png` | `true` | Save individual Canva slides as `.png` images. |
| `save_comments_md` | `true` | Save post/lesson comments and thread replies as Markdown (`.md`). |
| `save_comments_json` | `true` | Save post/lesson comments and thread replies as structured JSON (`.json`). |
| `verify_existing_files` | `false` | Verify local attachment file sizes against the server's `Content-Length` header and resume incomplete files. |
| `create_media_dump` | `true` | Create a `media_dump.md` file listing all extracted links, attachment URLs, and video stream URLs. |
| `create_link_status` | `true` | Generate a live Markdown status checklist (`Download_status_*.md`) for all discovered lessons and files. |
| `preserve_sidebar_section_structure` | `true` | Mirror the platform's `Section -> Space/Tab` folder hierarchy on disk. |
| `group_into_modules` | `true` | Group course lessons into subfolders named after their respective course modules. |
| `headless` | `false` | Run the Playwright browser in headless background mode (`true`) or visible mode (`false`). |
| `max_concurrent_downloads` | `2` | Number of parallel threads used for downloading attachments. |
| `ignored_titles` | `[...]` | List of title substrings to skip during scraping. |
| `download_external_videos` | `false` | Download videos hosted on external platforms (YouTube, Vimeo, etc.). If `false`, only native Circle/Cloudflare videos are downloaded. |
| `download_embedded_videos_posts` | `true` | Download embedded video players found inside the main post/lesson body. |
| `download_embedded_videos_comments` | `true` | Download embedded video players found inside comments. |
| `download_linked_videos_posts` | `false` | Download video URLs linked via standard `<a href>` tags in the post body. |
| `download_linked_videos_comments` | `false` | Download video URLs linked via standard `<a href>` tags in comments. |
| `max_post_name_length` | `50` | Maximum character length for lesson/post folder names and file prefixes. |
| `max_category_name_length` | `120` | Maximum character length for section, tab, and module folder names. |
| `backward_compatible_names` | `true` | Match existing local folders even if title truncation limits were previously different. |
| `fallback_username` | `"Me"` | Fallback author name used when a comment is labeled as "You" and the profile name cannot be read from DOM. |
| `download_lesson_thumbnails` | `false` | Download lesson thumbnail images from the course overview list. |
| `keep_state_on_completion` | `true` | Retain `progress_state.json` after finishing all tasks to quickly skip completed URLs on future runs. |
| `restore_state_from_local_folders` | `true` | Scan local directories on startup to populate `progress_state.json` with already downloaded lessons. |

---

## Localization (`locales.json`)

All console messages, status labels, Markdown headers, and output file suffixes are stored in `locales.json`.
* Set `"language": "en"` in `config.json` for English console output and English filenames (`content.md`, `comments.md`, `Canva_Presentation_1`, etc.).
* Set `"language": "pl"` in `config.json` for Polish console output and Polish filenames (`tresc.md`, `komentarze.md`, `Prezentacja_Canva_1`, etc.).
* You can easily add new languages by adding a new language key (e.g., `"de"`, `"es"`) to `locales.json`.

---

## Disclaimer

This tool is intended strictly for personal archiving, educational purposes, and backing up content to which you have authorized access. Respect the Terms of Service and copyright policies of the respective content creators and communities.