"""Screenshot OCR → translate → SRT inserter (customtkinter GUI).

Run: python app.py
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import config
import srt_utils
from llm_engine import LLMError, ProviderCfg, test_connection, translate_image

FONT_BODY = ("Microsoft JhengHei", 13)
FONT_SMALL = ("Microsoft JhengHei", 11)
FONT_MONO = ("Consolas", 12)
FONT_TITLE = ("Microsoft JhengHei", 20, "bold")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("截圖 OCR 翻譯 → SRT 插入器")
        self.geometry("1000x720")
        self.minsize(820, 620)

        self._settings = config.load_settings()
        ctk.set_appearance_mode(self._settings.get("appearance_mode", "dark"))
        ctk.set_default_color_theme("blue")

        # runtime state
        self._image_rows: list[dict] = []   # {path, ts(float|None), ts_label}
        self._srt_path: str = ""
        self._srt_entries: list[dict] = []
        self._translations: list[tuple[float, str]] = []  # (ts, text)
        self._busy = False

        # ---- tabs ----
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self.tabs.add("  截圖與字幕  ")
        self.tabs.add("  API 設定  ")
        self.tabs.add("  Prompt 設定  ")
        self.tabs.add("  執行  ")
        self._build_images_tab(self.tabs.tab("  截圖與字幕  "))
        self._build_api_tab(self.tabs.tab("  API 設定  "))
        self._build_prompt_tab(self.tabs.tab("  Prompt 設定  "))
        self._build_run_tab(self.tabs.tab("  執行  "))

        self._sync_api_form()

    # ------------------------------------------------------------------ #
    #  Tab 1: screenshots & subtitle
    # ------------------------------------------------------------------ #
    def _build_images_tab(self, parent):
        sec = ctk.CTkLabel(parent, text="截圖上傳", font=FONT_TITLE, anchor="w")
        sec.pack(fill="x", padx=14, pady=(12, 4))

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(row, text="選擇截圖 (多選)", width=160, height=30,
                      font=FONT_BODY, command=self._pick_images).pack(side="left")
        ctk.CTkButton(row, text="清空列表", width=110, height=30,
                      font=FONT_BODY, command=self._clear_images).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(parent, text="檔名後綴 _HHMMSS.mmm 會被解析為時間軸起點。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"),
                     anchor="w").pack(fill="x", padx=14)

        hdr = ctk.CTkFrame(parent)
        hdr.pack(fill="x", padx=14, pady=(6, 0))
        ctk.CTkLabel(hdr, text="檔名", font=FONT_SMALL, width=560, anchor="w").pack(side="left", padx=8)
        ctk.CTkLabel(hdr, text="時間軸", font=FONT_SMALL, width=140, anchor="w").pack(side="left")

        self._img_scroll = ctk.CTkScrollableFrame(parent)
        self._img_scroll.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        ctk.CTkLabel(parent, text="目標 SRT 字幕檔", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(8, 4))
        srow = ctk.CTkFrame(parent, fg_color="transparent")
        srow.pack(fill="x", padx=14, pady=(0, 6))
        self._srt_var = ctk.StringVar(value="（尚未選擇）")
        ctk.CTkLabel(srow, textvariable=self._srt_var, font=FONT_BODY,
                     anchor="w").pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(srow, text="選擇 SRT", width=120, height=30,
                      font=FONT_BODY, command=self._pick_srt).pack(side="left")
        self._srt_info = ctk.StringVar(value="")
        ctk.CTkLabel(parent, textvariable=self._srt_info, font=FONT_SMALL,
                     text_color=("gray40", "#9aa0a6"), anchor="w").pack(fill="x", padx=14)

    def _pick_images(self):
        initial = self._settings.get("last_image_dir") or os.getcwd()
        paths = filedialog.askopenfilenames(
            initialdir=initial,
            filetypes=[("圖片", "*.jpg *.jpeg *.png *.bmp *.webp"), ("所有檔案", "*.*")],
            title="選擇截圖（可多選）",
        )
        if not paths:
            return
        self._settings["last_image_dir"] = str(Path(paths[0]).parent)
        config.patch_setting("last_image_dir", self._settings["last_image_dir"])
        for p in paths:
            ts = srt_utils.parse_filename_timestamp(p)
            label = srt_utils.sec_to_ts(ts) if ts is not None else "（無法解析）"
            self._image_rows.append({"path": p, "ts": ts, "ts_label": label})
        self._render_image_list()

    def _clear_images(self):
        self._image_rows.clear()
        self._render_image_list()

    def _render_image_list(self):
        for w in self._img_scroll.winfo_children():
            w.destroy()
        for i, row in enumerate(self._image_rows, 1):
            frame = ctk.CTkFrame(self._img_scroll, fg_color=("gray86", "gray17"))
            frame.pack(fill="x", pady=2)
            name = Path(row["path"]).name
            color = ("gray10", "white") if row["ts"] is not None else ("red", "#ff8a80")
            ctk.CTkLabel(frame, text=f"{i}. {name}", font=FONT_MONO,
                         width=560, anchor="w", text_color=color).pack(side="left", padx=8, pady=4)
            ctk.CTkLabel(frame, text=row["ts_label"], font=FONT_MONO,
                         width=140, anchor="w", text_color=color).pack(side="left")

    def _pick_srt(self):
        initial = self._settings.get("last_srt_path") or self._settings.get("last_image_dir") or os.getcwd()
        p = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[("SRT 字幕", "*.srt"), ("所有檔案", "*.*")],
            title="選擇目標 SRT",
        )
        if not p:
            return
        self._srt_path = p
        self._settings["last_srt_path"] = p
        config.patch_setting("last_srt_path", p)
        self._srt_var.set(p)
        try:
            self._srt_entries = srt_utils.parse_srt(p)
        except Exception as exc:
            self._srt_info.set(f"解析失敗：{exc}")
            self._srt_entries = []
            return
        n = len(self._srt_entries)
        if n:
            first = srt_utils.sec_to_ts(self._srt_entries[0]["start"])
            last = srt_utils.sec_to_ts(self._srt_entries[-1]["end"])
            self._srt_info.set(f"{n} 條字幕　範圍 {first} → {last}")
        else:
            self._srt_info.set("（SRT 中未解析到字幕）")

    # ------------------------------------------------------------------ #
    #  Tab 2: API settings
    # ------------------------------------------------------------------ #
    def _build_api_tab(self, parent):
        ctk.CTkLabel(parent, text="API 設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(parent, text="可設定多組供應商，於下方下拉選單快速切換。",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6"), anchor="w").pack(
            fill="x", padx=14)

        switch_row = ctk.CTkFrame(parent, fg_color="transparent")
        switch_row.pack(fill="x", padx=14, pady=(8, 6))
        ctk.CTkLabel(switch_row, text="供應商：", font=FONT_BODY).pack(side="left")
        self._provider_menu = ctk.CTkOptionMenu(switch_row, width=200, height=30,
                                                font=FONT_BODY,
                                                values=["（無）"],
                                                command=self._on_provider_switch)
        self._provider_menu.pack(side="left", padx=(6, 12))
        ctk.CTkButton(switch_row, text="測試連線", width=110, height=30,
                      font=FONT_BODY, command=self._on_test_connection).pack(side="left")

        form = ctk.CTkFrame(parent)
        form.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        form.grid_columnconfigure(1, weight=1)

        self._p_name = ctk.StringVar()
        self._p_url = ctk.StringVar()
        self._p_key = ctk.StringVar()
        self._p_model = ctk.StringVar()

        def add_field(row, label, var, show=None):
            ctk.CTkLabel(form, text=label, font=FONT_BODY, anchor="e").grid(
                row=row, column=0, sticky="e", padx=(10, 8), pady=8
            )
            entry = ctk.CTkEntry(form, textvariable=var, font=FONT_BODY, show=show)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=8)
            return entry

        add_field(0, "名稱：", self._p_name)
        add_field(1, "API 網址：", self._p_url)
        add_field(2, "API 金鑰：", self._p_key, show="•")
        add_field(3, "模型名稱：", self._p_model)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(btns, text="新增", width=90, height=30, font=FONT_BODY,
                      command=self._new_provider).pack(side="left")
        ctk.CTkButton(btns, text="儲存目前", width=110, height=30, font=FONT_BODY,
                      command=self._save_current_provider).pack(side="left", padx=(8, 0))
        ctk.CTkButton(btns, text="刪除", width=90, height=30, font=FONT_BODY,
                      fg_color="#b23b3b", hover_color="#8a2e2e",
                      command=self._delete_current_provider).pack(side="left", padx=(8, 0))
        ctk.CTkButton(btns, text="設為預設", width=110, height=30, font=FONT_BODY,
                      command=self._set_default_provider).pack(side="left", padx=(8, 0))

        self._test_status = ctk.StringVar(value="")
        self._test_label = ctk.CTkLabel(parent, textvariable=self._test_status,
                                        font=FONT_SMALL, anchor="w")
        self._test_label.pack(fill="x", padx=14, pady=(0, 10))

    def _provider_names(self) -> list[str]:
        return [p["name"] for p in self._settings.get("providers", [])]

    def _refresh_provider_menu(self, keep: str | None = None):
        names = self._provider_names() or ["（無）"]
        self._provider_menu.configure(values=names)
        sel = keep if keep in names else (names[0] if names != ["（無）"] else "（無）")
        self._provider_menu.set(sel)

    def _sync_api_form(self):
        self._refresh_provider_menu()
        p = config.get_default_provider()
        if p:
            self._provider_menu.set(p["name"])
            self._load_provider_into_form(p)

    def _load_provider_into_form(self, p: dict):
        self._p_name.set(p.get("name", ""))
        self._p_url.set(p.get("api_url", ""))
        self._p_key.set(p.get("api_key", ""))
        self._p_model.set(p.get("model", ""))

    def _on_provider_switch(self, name: str):
        for p in self._settings.get("providers", []):
            if p.get("name") == name:
                self._load_provider_into_form(p)
                break

    def _form_to_provider(self) -> dict:
        return {
            "name": self._p_name.get().strip(),
            "api_url": self._p_url.get().strip(),
            "api_key": self._p_key.get().strip(),
            "model": self._p_model.get().strip(),
            "is_default": False,
        }

    def _new_provider(self):
        self._p_name.set("")
        self._p_url.set("https://api.openai.com/v1/chat/completions")
        self._p_key.set("")
        self._p_model.set("gpt-4o")

    def _save_current_provider(self):
        p = self._form_to_provider()
        if not p["name"]:
            messagebox.showwarning("缺少名稱", "請先填入供應商「名稱」。")
            return
        if not p["api_url"] or not p["model"]:
            messagebox.showwarning("資料不完整", "API 網址與模型名稱為必填。")
            return
        # preserve is_default if editing existing
        for ex in self._settings.get("providers", []):
            if ex.get("name") == p["name"]:
                p["is_default"] = ex.get("is_default", False)
                break
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=p["name"])

    def _delete_current_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        if not messagebox.askyesno("刪除供應商", f"確定刪除「{name}」？"):
            return
        config.delete_provider(name)
        self._settings = config.load_settings()
        self._sync_api_form()

    def _set_default_provider(self):
        name = self._p_name.get().strip()
        if not name:
            return
        p = self._form_to_provider()
        p["is_default"] = True
        config.upsert_provider(p)
        self._settings = config.load_settings()
        self._refresh_provider_menu(keep=name)

    def _on_test_connection(self):
        p = self._form_to_provider()
        if not p["api_url"] or not p["model"]:
            messagebox.showwarning("資料不完整", "請先填入 API 網址與模型名稱。")
            return
        cfg = ProviderCfg.from_dict(p)
        self._test_status.set("測試中…")
        self._test_label.configure(text_color=("#555", "#9aa0a6"))

        def work():
            ok, msg = test_connection(cfg)
            self.after(0, lambda: self._show_test_result(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _show_test_result(self, ok: bool, msg: str):
        self._test_status.set(("✅ " if ok else "❌ ") + msg)
        self._test_label.configure(text_color=("#0a7d2c", "#5ec97a") if ok else ("#b23b3b", "#ff8a80"))

    # ------------------------------------------------------------------ #
    #  Tab 3: prompt settings
    # ------------------------------------------------------------------ #
    def _build_prompt_tab(self, parent):
        ctk.CTkLabel(parent, text="Prompt 與翻譯設定", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        lang_row = ctk.CTkFrame(parent, fg_color="transparent")
        lang_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(lang_row, text="目標語言：", font=FONT_BODY).pack(side="left")
        self._lang_var = ctk.StringVar(value=self._settings.get("target_lang", config.DEFAULT_TARGET_LANG))
        self._lang_menu = ctk.CTkOptionMenu(lang_row, width=180, height=30,
                                            font=FONT_BODY, values=config.TARGET_LANG_PRESETS,
                                            variable=self._lang_var,
                                            command=self._on_lang_change)
        self._lang_menu.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(lang_row, text="或自訂：", font=FONT_BODY).pack(side="left")
        self._lang_custom = ctk.StringVar(value=self._settings.get("target_lang_custom", ""))
        entry = ctk.CTkEntry(lang_row, textvariable=self._lang_custom, width=180,
                             font=FONT_BODY)
        entry.pack(side="left", padx=(6, 0))
        entry.bind("<FocusOut>", lambda _e: config.patch_setting("target_lang_custom", self._lang_custom.get()))

        # system prompt
        ctk.CTkLabel(parent, text="System Prompt", font=FONT_BODY, anchor="w").pack(
            fill="x", padx=14, pady=(6, 2))
        self._sys_box = ctk.CTkTextbox(parent, height=110, font=FONT_BODY)
        self._sys_box.pack(fill="x", expand=False, padx=14)
        self._sys_box.insert("1.0", self._settings.get("system_prompt", config.DEFAULT_SYSTEM_PROMPT))

        # user prompt
        ur = ctk.CTkFrame(parent, fg_color="transparent")
        ur.pack(fill="x", padx=14, pady=(8, 2))
        ctk.CTkLabel(ur, text="User Prompt（{lang} 會自動帶入目標語言）", font=FONT_BODY,
                     anchor="w").pack(side="left")
        ctk.CTkButton(ur, text="恢復預設", width=90, height=26, font=FONT_SMALL,
                      command=self._reset_prompts).pack(side="right")
        self._usr_box = ctk.CTkTextbox(parent, height=110, font=FONT_BODY)
        self._usr_box.pack(fill="x", expand=False, padx=14)
        self._usr_box.insert("1.0", self._settings.get("user_prompt", config.DEFAULT_USER_PROMPT))

        # insertion mode
        mode_row = ctk.CTkFrame(parent, fg_color="transparent")
        mode_row.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(mode_row, text="插入方式：", font=FONT_BODY).pack(side="left")
        self._mode_var = ctk.StringVar(value=self._settings.get("insertion_mode", "insert"))
        self._mode_seg = ctk.CTkSegmentedButton(
            mode_row, values=["insert", "append"],
            command=lambda v: self._mode_var.set(v),
            variable=self._mode_var, height=30, font=FONT_BODY)
        self._mode_seg.pack(side="left", padx=(6, 12))
        ctk.CTkLabel(mode_row,
                     text="insert=插入新字幕　append=附加到涵蓋該時間軸的現有字幕",
                     font=FONT_SMALL, text_color=("gray40", "#9aa0a6")).pack(side="left")

        ctk.CTkButton(parent, text="儲存 Prompt 設定", width=140, height=30, font=FONT_BODY,
                      command=self._save_prompts).pack(padx=14, pady=(8, 10), anchor="w")

    def _current_lang(self) -> str:
        custom = self._lang_custom.get().strip()
        return custom or self._lang_var.get()

    def _on_lang_change(self, _v):
        config.patch_setting("target_lang", self._lang_var.get())

    def _reset_prompts(self):
        self._sys_box.delete("1.0", "end")
        self._sys_box.insert("1.0", config.DEFAULT_SYSTEM_PROMPT)
        self._usr_box.delete("1.0", "end")
        self._usr_box.insert("1.0", config.DEFAULT_USER_PROMPT)

    def _save_prompts(self):
        config.patch_setting("system_prompt", self._sys_box.get("1.0", "end").strip("\n"))
        config.patch_setting("user_prompt", self._usr_box.get("1.0", "end").strip("\n"))
        config.patch_setting("target_lang", self._lang_var.get())
        config.patch_setting("target_lang_custom", self._lang_custom.get().strip())
        config.patch_setting("insertion_mode", self._mode_var.get())
        self._settings = config.load_settings()

    # ------------------------------------------------------------------ #
    #  Tab 4: run
    # ------------------------------------------------------------------ #
    def _build_run_tab(self, parent):
        ctk.CTkLabel(parent, text="執行 OCR 翻譯與 SRT 插入", font=FONT_TITLE, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 8))
        self._run_btn = ctk.CTkButton(row, text="開始 OCR 翻譯", width=160, height=32,
                                      font=FONT_BODY, command=self._start_ocr)
        self._run_btn.pack(side="left")
        self._insert_btn = ctk.CTkButton(row, text="插入至 SRT 並另存", width=180, height=32,
                                         font=FONT_BODY, state="disabled",
                                         command=self._insert_and_save)
        self._insert_btn.pack(side="left", padx=(8, 0))

        self._progress = ctk.CTkProgressBar(parent, width=400)
        self._progress.set(0)
        self._progress.pack(fill="x", padx=14, pady=(0, 8))
        self._progress.set(0)

        self._log = ctk.CTkTextbox(parent, font=FONT_MONO, height=160)
        self._log.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # preview table
        ctk.CTkLabel(parent, text="翻譯結果預覽", font=FONT_BODY, anchor="w").pack(
            fill="x", padx=14)
        self._preview_scroll = ctk.CTkScrollableFrame(parent, height=140)
        self._preview_scroll.pack(fill="both", expand=False, padx=14, pady=(0, 10))

    def _log_line(self, msg: str):
        self.after(0, lambda: self._log.insert("end", msg + "\n"))
        self.after(0, lambda: self._log.see("end"))

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.after(0, lambda: self._run_btn.configure(state=state))

    def _start_ocr(self):
        if self._busy:
            return
        # gather config
        provider = config.get_default_provider()
        if not provider or not provider.get("api_key"):
            # fall back to whatever is currently selected/edited in the form
            provider = self._form_to_provider()
        if not provider or not provider.get("api_url") or not provider.get("model"):
            messagebox.showwarning("API 未設定", "請先在「API 設定」填入網址、模型與金鑰。")
            return
        rows = [r for r in self._image_rows if r["ts"] is not None]
        if not rows:
            messagebox.showwarning("無截圖", "請先在「截圖與字幕」選擇含時間軸檔名的截圖。")
            return
        self._save_prompts()  # persist current prompts/lang/mode
        cfg = ProviderCfg.from_dict(provider)
        sys_prompt = self._sys_box.get("1.0", "end").strip("\n") or config.DEFAULT_SYSTEM_PROMPT
        usr_prompt = self._usr_box.get("1.0", "end").strip("\n") or config.DEFAULT_USER_PROMPT
        lang = self._current_lang()
        max_side = int(self._settings.get("image_max_side", 1568))

        self._translations = []
        self._log.delete("1.0", "end")
        for w in self._preview_scroll.winfo_children():
            w.destroy()
        self._progress.set(0)
        self._insert_btn.configure(state="disabled")
        self._set_busy(True)

        def work():
            total = len(rows)
            for i, r in enumerate(rows, 1):
                self._log_line(f"[{i}/{total}] {Path(r['path']).name}  →  {r['ts_label']}")
                try:
                    text = translate_image(cfg, r["path"], sys_prompt, usr_prompt, lang, max_side)
                except LLMError as exc:
                    self._log_line(f"    ❌ 失敗：{exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._log_line(f"    ❌ 例外：{exc}")
                    continue
                preview = text.replace("\n", " ⏎ ") if text else "（無文字）"
                self._log_line(f"    ✅ {preview}")
                if text:
                    self._translations.append((r["ts"], text))
                    self.after(0, lambda r=r, text=text: self._add_preview(r["ts_label"], text))
                self._progress.set(i / total)
            self._log_line("OCR 翻譯完成。")
            self.after(0, lambda: self._insert_btn.configure(state="normal"))
            self._set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    def _add_preview(self, ts_label: str, text: str):
        frame = ctk.CTkFrame(self._preview_scroll, fg_color=("gray86", "gray17"))
        frame.pack(fill="x", pady=2)
        ctk.CTkLabel(frame, text=ts_label, font=FONT_MONO, width=130, anchor="w").pack(side="left", padx=8, pady=4)
        ctk.CTkLabel(frame, text=text.replace("\n", " / "), font=FONT_BODY,
                     anchor="w", wraplength=640).pack(side="left", fill="x", expand=True)

    def _insert_and_save(self):
        if not self._translations:
            messagebox.showinfo("無翻譯", "尚無可插入的翻譯結果。")
            return
        if not self._srt_entries:
            if not self._srt_path:
                messagebox.showwarning("無 SRT", "請先在「截圖與字幕」選擇目標 SRT 檔。")
                return
        mode = self._mode_var.get()
        entries = srt_utils.insert_translations(
            list(self._srt_entries), list(self._translations), mode=mode)
        orig = Path(self._srt_path) if self._srt_path else Path(self._settings.get("last_srt_path", ""))
        default_name = ""
        if orig.name:
            default_name = orig.stem + "_translated.srt"
        out = filedialog.asksaveasfilename(
            defaultextension=".srt",
            initialdir=str(orig.parent) if str(orig.parent) else os.getcwd(),
            initialfile=default_name,
            filetypes=[("SRT 字幕", "*.srt")],
            title="另存新檔",
        )
        if not out:
            return
        try:
            srt_utils.write_srt(out, entries)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("寫檔失敗", str(exc))
            return
        new_count = len(entries) - len(self._srt_entries)
        messagebox.showinfo("完成",
            f"已儲存：{out}\n共 {len(entries)} 條字幕"
            f"（新增/調整 {new_count} 條，模式：{mode}）")
        self._log_line(f"已另存：{out}（{len(entries)} 條，模式 {mode}）")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
