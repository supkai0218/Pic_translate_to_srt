# AV-Manager 與 PicTrans2Srt 整合指令與開發指南

本指南旨在提供 **AV-Manager** 在後續版本中串接 **PicTrans2Srt (v1.06+)** 的技術規格、呼叫指令語法及後端/前端整合範例。

---

## 一、 整合概述

**PicTrans2Srt** 支援標準 Windows 命令列參數，可以作為 AV-Manager 的高階播放器與字幕辨識輔助工具。

### 核心特性
1. **即刻播放 (Instant Playback)**：由外部傳入影片時預設自動開始播放（支援 `--no-play` 強制暫停）。
2. **自動配對字幕**：若未指定字幕檔案，PicTrans2Srt 會自動尋找影片同目錄下的同名或前綴相符的 `.srt` 字幕。
3. **無縫相容現有架構**：支援位置參數呼叫，即使 AV-Manager 不修改呼叫方式，僅將播放器路徑指向 `PicTrans2Srt.exe` 也能直接運作。

---

## 二、 命令列參數規範 (CLI Specifications)

### 語法格式
```text
PicTrans2Srt.exe [OPTIONS] [VIDEO_FILE]
```

### 參數對照表

| 參數選項 | 簡寫 | 參數值 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- | :--- |
| `video_file` | 無 | 影片絕對路徑 | `None` | **位置參數**。相容 Windows「以此程式開啟」與檔案拖曳啟動。 |
| `--video` | `-v` | 影片絕對路徑 | `None` | **具名參數**。指定欲播放的影片檔案。 |
| `--srt` | `-s` | 字幕絕對路徑 | `None` | **具名參數**。指定字幕檔案路徑（若省略則自動搜尋同名字幕）。 |
| `--play` | `-p` | 無 | `True`* | 載入後立即開始播放。 |
| `--no-play` | 無 | 無 | `False` | 載入後維持暫停狀態，不自動播放。 |
| `--help` | `-h` | 無 | - | 顯示說明訊息並退出。 |

> \* 註：當外部有傳入影片路徑時，預設自動播放；除非使用者明確指定 `--no-play`。

---

## 三、 呼叫範例

### 範例 1：最簡呼叫 (相容現有 AV-Manager 播放機制)
直接傳入影片路徑，PicTrans2Srt 會自動搜尋同名字幕並開始播放：
```powershell
PicTrans2Srt.exe "D:\Videos\MIDE-123\MIDE-123.mp4"
```

### 範例 2：完整具名參數 (指定特定字幕檔)
當 AV-Manager 知道專屬的翻譯字幕檔案位置時，可明確傳入：
```powershell
PicTrans2Srt.exe -v "D:\Videos\MIDE-123\MIDE-123.mp4" -s "D:\Videos\MIDE-123\MIDE-123_zh.srt"
```

### 範例 3：載入但不播放 (定位至 00:00 暫停)
```powershell
PicTrans2Srt.exe -v "D:\Videos\MIDE-123\MIDE-123.mp4" --no-play
```

---

## 四、 AV-Manager 後端 Python (Flask) 整合實作

AV-Manager 目前在 `app.py` 的 `/api/video/<int:video_id>/play` 中使用 `subprocess.Popen([player_path, actual])`。

以下為推薦的整合升級範例：

### 1. 後端播放函式升級範例 (`app.py`)

```python
import os
import subprocess
from pathlib import Path

def launch_pic_trans_player(player_exe: str, video_path: str, srt_path: str = None, autoplay: bool = True):
    """以非阻塞、進程分離方式啟動 PicTrans2Srt 播放器。"""
    if not os.path.isfile(player_exe):
        raise FileNotFoundError(f"找不到 PicTrans2Srt 執行檔: {player_exe}")
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"找不到影片檔案: {video_path}")

    # 組裝執行參數
    cmd = [player_exe, "--video", str(Path(video_path).resolve())]

    # 若有指定字幕檔案則加入 -s 參數
    if srt_path and os.path.isfile(srt_path):
        cmd.extend(["--srt", str(Path(srt_path).resolve())])

    if not autoplay:
        cmd.append("--no-play")

    # Windows 下使用 DETACHED_PROCESS 讓播放器獨立運作，避免關閉 AV-Manager 時播放器被連帶中斷
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        cmd,
        cwd=str(Path(player_exe).parent),  # 確保工作目錄為 exe 所在資料夾，讓 vlc 插件順利載入
        creationflags=creation_flags,
        close_fds=True
    )
```

### 2. 路由 API 整合建議

在 AV-Manager 的影片播放 API 中，可加入以下邏輯：

```python
@app.route("/api/video/<int:video_id>/play", methods=["POST"])
def play_video_by_id(video_id):
    # 1. 取得影片實體路徑 actual_path
    # 2. 搜尋資料庫或同目錄下是否有對應的 .srt 字幕
    data = request.get_json(silent=True) or {}
    use_pic_trans = data.get("use_pic_trans", False) # 支援前端按鈕指定播放器

    settings = load_settings()
    player_path = settings.get("pic_trans_path") if use_pic_trans else settings.get("player_path", "")

    # 自動偵測是否有伴隨字幕
    video_stem = Path(actual_path).stem
    video_dir = Path(actual_path).parent
    matched_srt = None
    for cand in [video_dir / f"{video_stem}.srt", video_dir / f"{video_stem}_zh.srt"]:
        if cand.is_file():
            matched_srt = str(cand)
            break

    # 若設定指向 PicTrans2Srt.exe
    if player_path and "PicTrans2Srt" in Path(player_path).name:
        launch_pic_trans_player(player_path, actual_path, srt_path=matched_srt, autoplay=True)
        return jsonify({"success": True, "message": "已在 PicTrans2Srt 中開啟"})

    # 原有播放邏輯（系統預設或其他播放器）
    ...
```

---

## 五、 AV-Manager 前端介面設計建議

### 1. 設定頁面 (Settings)
建議在設定頁面新增：
* **PicTrans2Srt 程式路徑**：
  * 輸入框：例如 `D:\Tools\PicTrans2Srt\PicTrans2Srt.exe`
  * 「瀏覽...」按鈕：透過 `filedialog` 選取 `.exe`
  * 「設為預設播放器」核取方塊。

### 2. 片單卡片 / 詳情頁按鈕
在每一部影片卡片的動作按鈕中：
* **一般播放**：點擊使用系統預設或原有的播放器。
* **OCR 字幕播放器 (PicTrans2Srt)**：
  * 可配置專屬小圖示（例如 `🔤` 或 `字幕翻譯`）。
  * 點擊時送出 POST 請求 `{ "use_pic_trans": true }`，直接喚起 PicTrans2Srt。
  * 對於無字幕的日韓影片，使用者可直接點此按鈕開啟進行逐句 OCR 辨識與字幕製作。

---

## 六、 發布結構與注意事項

在部署發布時，請確保 PicTrans2Srt 保持完整資料夾結構：
```text
PicTrans2Srt/
├── PicTrans2Srt.exe        <-- AV-Manager 指定的目標路徑
├── settings.json           <-- 保持可寫入
├── vlc/                    <-- LibVLC 核心與 plugins（不能刪除或移位）
│   ├── libvlc.dll
│   ├── libvlccore.dll
│   └── plugins/
└── _internal/              <-- 核心程式相依庫
```

> [!WARNING]
> 請勿將 `PicTrans2Srt.exe` 單獨複製至其他目錄執行，若脫離同目錄下的 `vlc/` 與 `_internal/`，播放器將無法正常載入影片硬體加速解碼模組。

