# Pic_translate_to_srt — 專案架構說明

> 本檔案供 AI agent 快速理解專案架構。切換 agent 或重啟 session 時，**先讀本檔**，不需重新掃描資料夾與所有程式碼。
> 後續改版時請同步維護本檔（見文末〈維護守則〉）。

## 一句話概述

桌面 GUI 工具：從影片截圖（檔名含 `_HHMMSS.mmm` 時間戳）出發，送 vision LLM 做 OCR + 翻譯，再將翻譯文字插入目標 SRT 字幕檔的正確時間軸。用於翻譯燒在畫面上的字幕（如日文字幕）。

- **語言**：Python 3.10+（使用 `from __future__ import annotations`）
- **UI 框架**：customtkinter（Tkinter 的現代主題封裝，非 Web/Gradio）
- **入口**：`python app.py`
- **目前版本**：v0.02

## 快速啟動

```bash
# 在專案根目錄
.venv/Scripts/python.exe app.py     # Windows
# 或已啟動 venv：python app.py
```

相依套件（`requirements.txt`）：`customtkinter`、`Pillow`、`requests`。

## 目錄結構

```
Pic_translate_to_srt/
├── .agent/
│   └── PROJECT_OVERVIEW.md   ← 本檔
├── .gitignore                # 忽略 .venv/、__pycache__/、source/、(settings.json 已被註解掉)
├── requirements.txt
├── settings.json             # 執行時設定（providers、prompts、duration 等）；首次啟動自動產生
├── config.py                 # 設定持久化（JSON）、供應商管理、預設值
├── srt_utils.py              # SRT 解析/寫檔、時間戳轉換、檔名時間戳解析、翻譯插入邏輯
├── llm_engine.py             # OpenAI 相容 vision LLM 客戶端（編碼圖片、API 呼叫）
├── app.py                    # GUI 應用（customtkinter）、4 個分頁、執行緒
└── source/                   # 測試素材（原始 SRT、輸出 SRT、截圖 JPG）；已被 .gitignore 忽略
```

## 模組職責

| 檔案 | 行數 | 職責 |
|---|---|---|
| `app.py` | ~650 | GUI 全部邏輯：4 分頁、檔案對話框、背景執行緒、OCR/插入流程 |
| `config.py` | ~145 | `settings.json` 讀寫、`_deep_merge` 補預設值、供應商 CRUD |
| `srt_utils.py` | ~177 | 時間戳轉換、SRT parse/write、檔名時間戳解析、`insert_translations()` |
| `llm_engine.py` | ~156 | `ProviderCfg` dataclass、`encode_image()`、`translate_image()`、`test_connection()` |

架構為乾淨分層：`app.py`（UI）→ `srt_utils.py`（字幕邏輯）+ `llm_engine.py`（API）+ `config.py`（設定）。`llm_engine.py` 與 `srt_utils.py` 不互相匯入。

## app.py 內部結構

`App(ctk.CTk)` 為主視窗（1000×720，最小 820×620，深色模式），4 個分頁：

| 分頁 | 建構方法 | 功能 |
|---|---|---|
| 截圖與字幕 | `_build_images_tab` | 多選截圖、解析檔名時間戳、可滾動列表（每列含檔名/時間軸/**秒數**欄位）、選擇目標 SRT |
| API 設定 | `_build_api_tab` | 多供應商管理、測試連線、設為預設 |
| Prompt 設定 | `_build_prompt_tab` | 目標語言、system/user prompt、插入方式（insert/append） |
| 執行 | `_build_run_tab` | 進度條、日誌、翻譯預覽、開始 OCR、插入並另存、**字幕時間長度模式選擇器** |

**runtime state（`__init__`）**：
- `self._image_rows: list[dict]` — 每列 `{path, ts, ts_label, duration, dur_entry, dur_var}`
- `self._srt_path` / `self._srt_entries` — 目標 SRT 路徑與解析後條目
- `self._translations: list[tuple[float, str]]` — OCR 結果 `(ts, text)`
- `self._translation_rows: list[dict]` — 與 `_translations` 平行，指向產生該翻譯的 image row（**v0.02 新增**，供 per_subtitle 即時讀秒數）
- `self._busy` — 是否執行中

**關鍵方法**：
- `_pick_images()` / `_render_image_list()` — 載入截圖、渲染列表（每列含秒數 entry）
- `_start_ocr()` — 背景執行緒跑 OCR，結果存入 `_translations` + `_translation_rows`
- `_insert_and_save()` — 呼叫 `srt_utils.insert_translations()` + `write_srt()` 另存
- `_get_duration_value()` / `_on_duration_mode_change()` / `_refresh_duration_ui()` — 時間長度模式 UI 同步

## 核心資料流

```
Tab1 選截圖 → _image_rows[{path, ts, ts_label, duration, ...}]
                          │
Tab4 選 duration_mode（auto/unified/per_subtitle）+ unified 時輸入秒數
                          │
_start_ocr() 背景執行緒：對每張圖 translate_image()
   → _translations[(ts, text)] + _translation_rows[row_ref]
   → 預覽區顯示翻譯
                          │
（per_subtitle 模式下，使用者可回 Tab1 修改每列秒數 → row["duration"] 即時更新）
                          │
_insert_and_save()：依 duration_mode 組裝 translations
   → srt_utils.insert_translations(entries, translations, mode, duration_mode, duration)
   → srt_utils.write_srt() 另存 *_translated.srt
```

## settings.json 結構

由 `config.py` 的 `_DEFAULTS` 定義，`_deep_merge()` 會自動補舊檔缺少的 key：

| key | 預設 | 說明 |
|---|---|---|
| `providers` | [OpenAI] | 供應商清單：`{name, api_url, api_key, model, is_default}` |
| `default_provider` | "OpenAI" | 預設供應商名稱 |
| `target_lang` / `target_lang_custom` | "繁體中文" / "" | 目標語言（預設集 + 自訂） |
| `system_prompt` / `user_prompt` | 見 config.py | OCR+翻譯 prompt；user_prompt 含 `{lang}` 佔位 |
| `insertion_mode` | "insert" | `"insert"`（新增字幕）/ `"append"`（附加到現有字幕） |
| `duration_mode` | "auto" | `"auto"` / `"unified"` / `"per_subtitle"`（v0.01 新增） |
| `subtitle_duration` | 2.0 | unified 模式的統一秒數（v0.01 新增） |
| `image_max_side` | 1568 | 圖片縮放最長邊 |
| `last_image_dir` / `last_srt_path` | "" | 記憶目錄 |
| `appearance_mode` | "dark" | 介面主題 |

`TARGET_LANG_PRESETS = ["繁體中文", "簡體中文", "English", "日本語", "한국어"]`

## 關鍵概念

### 時間戳
- 截圖檔名後綴 `_HHMMSS.mmm` → `srt_utils.parse_filename_timestamp()` → 秒數（float）。例：`...mp4_000213.789.jpg` → 133.789 秒。
- SRT 時間戳 `HH:MM:SS,mmm` ↔ 秒數：`ts_to_sec()` / `sec_to_ts()`。

### 插入方式（insertion_mode）
- **insert**：為每個翻譯建立新字幕條目，`start=ts`。
- **append**：將翻譯附加到時間軸涵蓋 `ts` 的現有字幕；落在空隙則退回 insert。

### 字幕時間長度（duration_mode，v0.01 新增）
決定 insert 模式下新字幕的 `end` 時間：
- **auto**：`end = 下一個字幕的 start`，無則 `ts+2.0`（v0.0 原行為，`_make_new_entry()`）。
- **unified**：`end = ts + subtitle_duration`（全部字幕同一秒數，Tab4 輸入，支援小數）。
- **per_subtitle**：`end = ts + 該字幕的秒數`（每張截圖在 Tab1 各自設定秒數）。

> **v0.02 重點**：per_subtitle 的秒數在 `_insert_and_save()` 時**即時**從 `_translation_rows` 讀取（非 OCR 時快照），故 OCR 後回 Tab1 修改秒數再寫入 SRT 仍會生效。`_translations` 一律存 `(ts, text)` 二元組；秒數在插入時才組裝成三元組傳給 `srt_utils`。

### srt_utils.insert_translations() 簽名
```python
insert_translations(
    entries: list[dict],
    translations: list[tuple[float, str]] | list[tuple[float, str, float]],
    mode: str = "insert",            # "insert" | "append"
    duration_mode: str = "auto",    # "auto" | "unified" | "per_subtitle"
    duration: float = 2.0,          # unified 模式用
) -> list[dict]
```
內部會正規化 translations（容許 2 元組或 3 元組）、過濾空文字、排序、防呆 `end>start`（否則 +0.5s）。`_make_new_entry()` 僅 auto 模式使用。

## LLM 呼叫（llm_engine.py）

- 任何 OpenAI 相容 chat completions 端點皆可（OpenAI、Gemini OpenAI 端點、Groq、OpenRouter、LM Studio、Ollama OpenAI 模式）。
- `encode_image()`：Pillow 載入 → RGB → 縮放最長邊至 `max_side` → JPEG q88 → base64。
- `translate_image()`：組 multimodal payload（圖片以 `data:image/jpeg;base64,...` URL 內嵌），`temperature=0.2`，取 `choices[0].message.content`（容許 content 為 block list）。
- `test_connection()`：純文字 round-trip，回覆含 "ok" 視為成功。
- `LLMError`：API 失敗/格式不符時拋出。

## 維護守則（給 agent）

改版時請同步更新本檔：
1. **新增/移除檔案** → 更新〈目錄結構〉與〈模組職責〉表格。
2. **改動 `__init__` state 或關鍵方法** → 更新〈app.py 內部結構〉。
3. **改動 settings 欄位** → 更新〈settings.json 結構〉表格與 `config.py` 的 `_DEFAULTS`。
4. **改動插入/時間長度邏輯** → 更新〈核心資料流〉與〈關鍵概念〉，並標註版本。
5. **改動 LLM 呼叫** → 更新〈LLM 呼叫〉。
6. 每次更新後將「目前版本」註記（檔頭與下方版本歷史）。

### 版本歷史
- **v0.0**：初始。截圖 OCR → 翻譯 → SRT 插入（insert/append），`_make_new_entry()` 固定 `ts+2.0` fallback。
- **v0.01**：新增字幕時間長度三模式（auto/unified/per_subtitle）；Tab4 模式選擇器、Tab1 每列秒數欄位、`_refresh_duration_ui()` 同步。per_subtitle 於 OCR 時快照秒數。
- **v0.02**：新增 `.agent/PROJECT_OVERVIEW.md`（本檔）。per_subtitle 改為插入時即時讀秒數（新增 `_translation_rows` 平行清單），OCR 後仍可回 Tab1 修改秒數再寫入 SRT。
