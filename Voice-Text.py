#!/usr/bin/env python3
"""
会议录音转文字工具 - macOS
依赖: pip install mlx-whisper deep-translator opencc-python-reimplemented
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import subprocess
import threading
import os
import sys
import tempfile

# 支持的格式
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".ts", ".mts"}
ALL_EXTS   = AUDIO_EXTS | VIDEO_EXTS


def ensure_deps():
    for dep in ["deep-translator", "opencc-python-reimplemented"]:
        try:
            __import__(dep.replace("-", "_").split("-")[0])
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", dep], capture_output=True)

ensure_deps()


# ── 参数定义表 ────────────────────────────────────────────
# (flag, 说明, 默认值, 类型)
PARAMS = [
    ("--compression-ratio-threshold",
     "重复输出阈值 — 超过此值判定为重复内容并丢弃，越低越严格",
     "1.8", "float"),

    ("--hallucination-silence-threshold",
     "静音跳过阈值 — 静音持续超过此秒数则直接跳过，避免乱猜",
     "1.5", "float"),

    ("--no-speech-threshold",
     "无声判断门槛 — 越高越容易把一段判定为无声并跳过（范围 0~1）",
     "0.85", "float"),

    ("--logprob-threshold",
     "置信度门槛 — 模型对某段不确定时丢弃，负数越小越宽松",
     "-0.5", "float"),

    ("--condition-on-previous-text",
     "参考上文 — 设为 False 则每段独立识别，防止重复内容向后传染",
     "False", "str"),

    ("--initial-prompt",
     "初始提示语 — 告诉模型语言风格和场景，有助于提升准确率",
     "以下是普通话简体中文会议记录。", "str"),
]


class ParamRow:
    """一行可勾选 + 可编辑数值的参数控件"""
    def __init__(self, parent, flag, desc, default, kind, row):
        self.flag = flag
        self.kind = kind
        self.enabled = tk.BooleanVar(value=True)
        self.value   = tk.StringVar(value=default)

        ttk.Checkbutton(parent, variable=self.enabled).grid(
            row=row, column=0, padx=(4, 0), pady=4, sticky=tk.W)

        ttk.Label(parent, text=flag, font=("Courier", 11),
                  foreground="#1a6fa8").grid(
            row=row, column=1, sticky=tk.W, padx=(2, 10))

        ttk.Label(parent, text=desc, foreground="#555",
                  wraplength=360, justify=tk.LEFT).grid(
            row=row, column=2, sticky=tk.W, padx=(0, 10))

        ttk.Entry(parent, textvariable=self.value,
                  width=20, font=("Courier", 11)).grid(
            row=row, column=3, sticky=tk.W)

    def to_args(self):
        if not self.enabled.get():
            return []
        return [self.flag, self.value.get().strip()]


class WhisperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎙 会议录音转文字")
        self.root.geometry("960x800")
        self.root.resizable(True, True)
        self.process = None
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 12, "pady": 5}

        # ── 文件区 ──────────────────────────────────────
        ff = ttk.LabelFrame(self.root, text="  📁 文件  ", padding=10)
        ff.pack(fill=tk.X, **pad)
        ff.columnconfigure(1, weight=1)

        ttk.Label(ff, text="录音文件").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.input_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.input_var).grid(
            row=0, column=1, sticky=tk.EW, padx=8)
        ttk.Button(ff, text="选择…", width=8,
                   command=self.browse_input).grid(row=0, column=2)

        ttk.Label(ff, text="输出文件夹").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.output_var = tk.StringVar(value=os.path.expanduser("~/Desktop"))
        ttk.Entry(ff, textvariable=self.output_var).grid(
            row=1, column=1, sticky=tk.EW, padx=8)
        ttk.Button(ff, text="选择…", width=8,
                   command=self.browse_output).grid(row=1, column=2)

        # ── 语言 & 翻译 ─────────────────────────────────
        lf = ttk.LabelFrame(self.root, text="  🌐 语言与翻译  ", padding=10)
        lf.pack(fill=tk.X, **pad)

        ttk.Label(lf, text="会议语言").grid(row=0, column=0, sticky=tk.W)
        self.lang_var = tk.StringVar(value="zh")
        lb = ttk.Frame(lf)
        lb.grid(row=0, column=1, sticky=tk.W, padx=10)
        for t, v in [("中文", "zh"), ("英文", "en"), ("自动识别", "auto"), ("多语言混合", "mixed")]:
            ttk.Radiobutton(lb, text=t, variable=self.lang_var,
                            value=v).pack(side=tk.LEFT, padx=6)

        ttk.Label(lf, text="翻译为").grid(row=0, column=2, sticky=tk.W, padx=(30, 0))
        self.trans_var = tk.StringVar(value="en")
        tb = ttk.Frame(lf)
        tb.grid(row=0, column=3, sticky=tk.W, padx=10)
        for t, v in [("English", "en"), ("中文", "zh-CN"), ("不翻译", "none")]:
            ttk.Radiobutton(tb, text=t, variable=self.trans_var,
                            value=v).pack(side=tk.LEFT, padx=6)

        self.simp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(lf, text="繁体 → 简体",
                        variable=self.simp_var).grid(row=0, column=4, padx=20)

        # 增加多语言混合模式的候选范围输入
        ttk.Label(lf, text="多语言范围").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.mixed_langs_var = tk.StringVar(value="en, bn, zh")
        self.mixed_langs_entry = ttk.Entry(lf, textvariable=self.mixed_langs_var, width=25)
        self.mixed_langs_entry.grid(row=1, column=1, sticky=tk.W, padx=10, pady=(8, 0))
        ttk.Label(lf, text="（仅多语言混合模式生效，逗号分隔代码，如 en,bn,zh）",
                  foreground="#666", font=("Helvetica", 11)).grid(
            row=1, column=2, columnspan=3, sticky=tk.W, pady=(8, 0))

        # ── 参数区 ──────────────────────────────────────
        pf = ttk.LabelFrame(
            self.root,
            text="  ⚙️ 转录参数  （勾选 = 启用，数值可直接修改）  ",
            padding=10)
        pf.pack(fill=tk.X, **pad)
        pf.columnconfigure(2, weight=1)

        # 表头
        for col, h in enumerate(["启用", "参数名", "说明", "数值"]):
            ttk.Label(pf, text=h, foreground="#999",
                      font=("Helvetica", 10, "bold")).grid(
                row=0, column=col, sticky=tk.W, padx=4)
        ttk.Separator(pf, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=4, sticky=tk.EW, pady=3)

        self.param_rows = []
        for i, args in enumerate(PARAMS):
            pr = ParamRow(pf, *args, row=i + 2)
            self.param_rows.append(pr)

        # ── 按钮栏 ──────────────────────────────────────
        bf = ttk.Frame(self.root)
        bf.pack(fill=tk.X, padx=12, pady=6)

        self.start_btn = ttk.Button(bf, text="▶  开始转录",
                                    command=self.start, width=16)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(bf, text="⏹  停止",
                                   command=self.stop, width=10, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        self.status_lbl = ttk.Label(bf, text="", foreground="#555")
        self.status_lbl.pack(side=tk.LEFT, padx=14)

        self.progress = ttk.Progressbar(bf, mode="indeterminate", length=150)
        self.progress.pack(side=tk.RIGHT)

        # ── 输出标签页 ───────────────────────────────────
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
        self.log_txt   = self._tab("📟  终端日志")
        self.trans_txt = self._tab("📄  转录结果")
        self.tr_txt    = self._tab("🌐  翻译")

    def _tab(self, label):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=label)
        st = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Helvetica", 12))
        st.pack(fill=tk.BOTH, expand=True)
        return st

    # ── 文件选择 ─────────────────────────────────────────
    def browse_input(self):
        audio_pat = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        video_pat = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        all_pat   = " ".join(f"*{e}" for e in sorted(ALL_EXTS))
        f = filedialog.askopenfilename(
            title="选择音频 / 视频文件",
            filetypes=[("音频+视频", all_pat),
                       ("音频", audio_pat),
                       ("视频", video_pat),
                       ("所有", "*.*")])
        if f:
            self.input_var.set(f)

    def browse_output(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.output_var.set(d)

    # ── 控制 ─────────────────────────────────────────────
    def start(self):
        inp = self.input_var.get().strip()
        out = self.output_var.get().strip()
        if not inp:
            messagebox.showerror("提示", "请先选择录音文件"); return
        if not os.path.exists(inp):
            messagebox.showerror("提示", "找不到该文件"); return

        for w in (self.log_txt, self.trans_txt, self.tr_txt):
            w.delete("1.0", tk.END)

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress.start(12)
        self._status("转录中…")
        self.stop_requested = False
        threading.Thread(target=self._run, args=(inp, out), daemon=True).start()

    def stop(self):
        self.stop_requested = True
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        self._log("\n⏹ 已手动停止\n")
        self._status("已停止")
        self._done()

    # ── 转录线程 ─────────────────────────────────────────
    def _run(self, inp, out):
        lang = self.lang_var.get()
        tmp_audio = None          # 视频提取/多语言格式转换的临时音频路径

        # ── 视频或多语言混合？先提取/转换音轨 ──────────────────────────────
        ext = os.path.splitext(inp)[1].lower()
        is_video = ext in VIDEO_EXTS
        need_wav = is_video or lang == "mixed"
        if need_wav:
            self.root.after(0, self._log, f"🎬 正在提取/转换音频格式…\n")
            self.root.after(0, self._status, "转换音频中…")
            try:
                tmp_fd, tmp_audio = tempfile.mkstemp(suffix=".wav", prefix="vt_audio_")
                os.close(tmp_fd)
                ffcmd = [
                    "ffmpeg", "-y", "-i", inp,
                    "-vn",                  # 不要视频
                    "-acodec", "pcm_s16le", # 16-bit PCM
                    "-ar", "16000",         # 16 kHz
                    "-ac", "1",             # 单声道
                    tmp_audio
                ]
                result = subprocess.run(
                    ffcmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                if result.returncode != 0:
                    self.root.after(0, self._log,
                        f"\n❌ ffmpeg 转换失败：\n{result.stdout}\n")
                    self.root.after(0, self._done)
                    return
                self.root.after(0, self._log, "✅ 提取/转换音轨完成\n\n")
            except FileNotFoundError:
                self.root.after(0, self._log,
                    "\n❌ 找不到 ffmpeg，请先安装：brew install ffmpeg\n")
                self.root.after(0, self._done)
                return
            except Exception as e:
                self.root.after(0, self._log, f"\n❌ 转换音频时出错：{e}\n")
                self.root.after(0, self._done)
                return
            whisper_input = tmp_audio
        else:
            whisper_input = inp

        # ── 多语言混合模式 ─────────────────────────────
        if lang == "mixed":
            self.root.after(0, self._status, "多语言转录中…")
            self.root.after(0, self._log, "✨ 开始多语言混合识别 (逐段自动检测语言)...\n")

            import numpy as np
            import wave
            import mlx_whisper

            try:
                with wave.open(whisper_input, 'rb') as w:
                    params = w.getparams()
                    n_channels, sampwidth, framerate, n_frames = params[:4]
                    data = w.readframes(n_frames)
                    audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception as e:
                self.root.after(0, self._log, f"❌ 读取音频失败: {e}\n")
                if tmp_audio and os.path.exists(tmp_audio):
                    try:
                        os.remove(tmp_audio)
                    except OSError:
                        pass
                self.root.after(0, self._done)
                return

            split_points = self._find_split_points(audio_data, rate=16000)
            total_segments = len(split_points) - 1
            self.root.after(0, self._log, f"音频共切分为 {total_segments} 个片段进行识别。\n\n")

            model_name = "mlx-community/whisper-large-v3-turbo"
            
            # 解析配置参数
            decode_options = {}
            for pr in self.param_rows:
                if pr.enabled.get():
                    name = pr.flag.lstrip('-').replace('-', '_')
                    val_str = pr.value.get().strip()
                    if pr.kind == "float":
                        try:
                            decode_options[name] = float(val_str)
                        except ValueError:
                            pass
                    elif pr.kind == "int":
                        try:
                            decode_options[name] = int(val_str)
                        except ValueError:
                            pass
                    else:
                        if val_str.lower() == "true":
                            decode_options[name] = True
                        elif val_str.lower() == "false":
                            decode_options[name] = False
                        else:
                            decode_options[name] = val_str

            user_initial_prompt = decode_options.get("initial_prompt", "")
            prev_text = user_initial_prompt
            
            # 移除一些不支持或不适用的参数
            decode_options.pop("initial_prompt", None)

            full_lines = []
            orig_base = os.path.splitext(os.path.basename(inp))[0]
            txfile = os.path.join(out, orig_base + ".txt")

            for idx in range(total_segments):
                if self.stop_requested:
                    break

                start_sample = split_points[idx]
                end_sample = split_points[idx+1]
                chunk = audio_data[start_sample:end_sample]

                start_sec = start_sample / 16000.0
                end_sec = end_sample / 16000.0

                self.root.after(0, self._log, f"正在识别片段 {idx+1}/{total_segments} ({self._format_time(start_sec)} --> {self._format_time(end_sec)})...\n")

                chunk_opts = decode_options.copy()
                if chunk_opts.get("condition_on_previous_text", True):
                    if prev_text:
                        # 传递上文提示，为了限制长度，取最后的 200 字/字符
                        chunk_opts["initial_prompt"] = prev_text[-200:]
                else:
                    if idx == 0 and user_initial_prompt:
                        chunk_opts["initial_prompt"] = user_initial_prompt
                    else:
                        chunk_opts["initial_prompt"] = None

                # 限制检测语言逻辑
                allowed_langs = []
                langs_str = self.mixed_langs_var.get().strip()
                if langs_str:
                    allowed_langs = [l.strip().lower() for l in langs_str.replace("，", ",").split(",") if l.strip()]

                detected_lang = None
                if allowed_langs:
                    try:
                        import mlx.core as mx
                        from mlx_whisper.transcribe import ModelHolder, log_mel_spectrogram, pad_or_trim
                        dtype = mx.float16 if chunk_opts.get("fp16", True) else mx.float32
                        model = ModelHolder.get_model(model_name, dtype)
                        
                        # 计算当前分片的 mel 频谱并执行语言检测
                        mel = log_mel_spectrogram(chunk, n_mels=model.dims.n_mels, padding=480000)
                        mel_segment = pad_or_trim(mel, 3000, axis=-2).astype(dtype)
                        _, probs = model.detect_language(mel_segment)
                        
                        # 过滤允许的语言
                        probs_filtered = {k: v for k, v in probs.items() if k in allowed_langs}
                        if probs_filtered:
                            detected_lang = max(probs_filtered, key=probs_filtered.get)
                        else:
                            detected_lang = max(probs, key=probs.get)
                    except Exception as e:
                        self.root.after(0, self._log, f"⚠️ 限制语言检测出错: {e}，回退到自动检测。\n")
                        detected_lang = None

                # 转录当前片段
                try:
                    result = mlx_whisper.transcribe(
                        chunk,
                        path_or_hf_repo=model_name,
                        language=detected_lang,  # 传入我们限制后检测出的语言，如果为 None 则由 Whisper 完全自主判断
                        **chunk_opts
                    )
                except Exception as e:
                    self.root.after(0, self._log, f"⚠️ 片段 {idx+1} 识别出错: {e}\n")
                    continue

                segment_text = result.get("text", "").strip()
                detected_lang = result.get("language", detected_lang or "unknown")
                
                # 过滤幻觉重复（防 yeahyeahyeah, thank you for watching 循环）
                segment_text = self._filter_repetition_hallucination(segment_text)

                # 繁简转换
                if self.simp_var.get() and detected_lang in ("zh", "unknown"):
                    segment_text = self._to_simplified(segment_text)

                if segment_text:
                    prev_text = segment_text
                    lang_name = self.get_language_name(detected_lang)
                    line_formatted = f"[{self._format_time(start_sec)} --> {self._format_time(end_sec)}] [{lang_name}] {segment_text}"
                    self.root.after(0, self._log, f" -> {line_formatted}\n\n")
                    full_lines.append(line_formatted)
                else:
                    self.root.after(0, self._log, " -> (无声音、过滤的幻觉或未识别到内容)\n\n")

            # 拼接最终文本
            transcript = "\n".join(full_lines)
            
            # 清理临时文件
            if tmp_audio and os.path.exists(tmp_audio):
                try:
                    os.remove(tmp_audio)
                except OSError:
                    pass

            if self.stop_requested:
                self.root.after(0, self._status, "已停止")
                self.root.after(0, self._done)
                return

            # 写入结果文件
            try:
                with open(txfile, "w", encoding="utf-8") as f:
                    f.write(transcript)
            except Exception as e:
                self.root.after(0, self._log, f"❌ 写入输出文件失败: {e}\n")
                self.root.after(0, self._done)
                return

            self.root.after(0, self._show, self.trans_txt, transcript, 1)

            # 翻译（如果启用）
            tgt = self.trans_var.get()
            if tgt != "none":
                self.root.after(0, self._status, "翻译中…")
                translated = self._translate(transcript, tgt)
                if translated:
                    tr_file = os.path.join(out, orig_base + "_翻译.txt")
                    with open(tr_file, "w", encoding="utf-8") as f:
                        f.write(translated)
                    self.root.after(0, self._show, self.tr_txt, translated, 2)
                    self.root.after(0, self._log, f"\n✅ 翻译已保存：{tr_file}\n")

            self.root.after(0, self._status, "✅ 全部完成")
            self.root.after(0, self._done)
            return

        # ── 默认单语言模式（使用 subprocess 运行命令行） ─────────────────────────────
        cmd = ["mlx_whisper", whisper_input,
               "--model", "mlx-community/whisper-large-v3-turbo",
               "--output-format", "txt",
               "--output-dir", out]

        if lang == "zh":
            cmd += ["--language", "zh"]
        elif lang == "en":
            cmd += ["--language", "en"]

        for pr in self.param_rows:
            cmd += pr.to_args()

        self.root.after(0, self._status, "转录中…")
        self._log("运行命令：\n" + " \\\n  ".join(cmd) + "\n\n")

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env)
            for line in self.process.stdout:
                self.root.after(0, self._log, line)
            self.process.wait()
            rc = self.process.returncode
        except FileNotFoundError:
            self.root.after(0, self._log,
                "\n❌ 找不到 mlx_whisper，请运行：pip install mlx-whisper\n")
            self.root.after(0, self._done); return
        except Exception as e:
            self.root.after(0, self._log, f"\n❌ 错误：{e}\n")
            self.root.after(0, self._done); return

        # ── 清理临时音频文件 ───────────────────────────────
        if tmp_audio and os.path.exists(tmp_audio):
            try:
                os.remove(tmp_audio)
            except OSError:
                pass

        if rc != 0:
            self.root.after(0, self._status, "❌ 转录失败")
            self.root.after(0, self._done); return

        # 输出文件名以原始文件为准（视频时 whisper 用的是临时文件名）
        if tmp_audio:
            # whisper 输出文件名基于临时文件名
            tmp_base = os.path.splitext(os.path.basename(tmp_audio))[0]
            whisper_out = os.path.join(out, tmp_base + ".txt")
            # 重命名为原始文件名
            orig_base = os.path.splitext(os.path.basename(inp))[0]
            txfile = os.path.join(out, orig_base + ".txt")
            if os.path.exists(whisper_out):
                os.rename(whisper_out, txfile)
        else:
            orig_base = os.path.splitext(os.path.basename(inp))[0]
            txfile = os.path.join(out, orig_base + ".txt")

        if not os.path.exists(txfile):
            self.root.after(0, self._log, "\n❌ 未找到输出文件\n")
            self.root.after(0, self._done); return

        with open(txfile, encoding="utf-8") as f:
            transcript = f.read()

        base = orig_base  # 用于后续翻译文件命名

        if self.simp_var.get() and lang in ("zh", "auto"):
            transcript = self._to_simplified(transcript)
            with open(txfile, "w", encoding="utf-8") as f:
                f.write(transcript)

        self.root.after(0, self._show, self.trans_txt, transcript, 1)

        tgt = self.trans_var.get()
        if tgt != "none":
            self.root.after(0, self._status, "翻译中…")
            translated = self._translate(transcript, tgt)
            if translated:
                tr_file = os.path.join(out, base + "_翻译.txt")
                with open(tr_file, "w", encoding="utf-8") as f:
                    f.write(translated)
                self.root.after(0, self._show, self.tr_txt, translated, 2)
                self.root.after(0, self._log, f"\n✅ 翻译已保存：{tr_file}\n")

        self.root.after(0, self._status, "✅ 全部完成")
        self.root.after(0, self._done)

    def _find_split_points(self, audio_data, rate=16000, min_chunk_len=25, max_chunk_len=35):
        import numpy as np
        total_samples = len(audio_data)
        if total_samples <= max_chunk_len * rate:
            return [0, total_samples]
        
        split_points = [0]
        current = 0
        
        while current + max_chunk_len * rate < total_samples:
            start_idx = current + min_chunk_len * rate
            end_idx = current + max_chunk_len * rate
            
            search_area = audio_data[start_idx:end_idx]
            frame_len = int(0.1 * rate)
            min_energy = float('inf')
            best_split_idx = start_idx + len(search_area) // 2
            
            step = int(0.05 * rate)
            for offset in range(0, len(search_area) - frame_len, step):
                frame = search_area[offset : offset + frame_len]
                energy = np.sum(np.abs(frame))
                if energy < min_energy:
                    min_energy = energy
                    best_split_idx = start_idx + offset + frame_len // 2
                    
            split_points.append(best_split_idx)
            current = best_split_idx
            
        split_points.append(total_samples)
        return split_points

    def _format_time(self, seconds):
        m = int(seconds // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{m:02d}:{s:02d}.{ms:03d}"

    def get_language_name(self, code):
        mapping = {
            "zh": "中文",
            "en": "英语",
            "ja": "日语",
            "ko": "韩语",
            "de": "德语",
            "fr": "法语",
            "es": "西班牙语",
            "ru": "俄语",
            "it": "意大利语",
            "bn": "孟加拉语",
        }
        return mapping.get(code, code)

    def _filter_repetition_hallucination(self, text):
        text_lower = text.lower().strip()
        if not text_lower:
            return ""
            
        # 1. 过滤单纯的大量重复英文单词 (如 yeah yeah yeah yeah yeah)
        words = text_lower.split()
        if len(words) > 4:
            unique_words = set(words)
            if len(unique_words) <= 2:
                # 只有少于等于 2 个不同的词（如全是 yeah 或 yeah/oh），直接判定为幻觉丢弃
                return ""

        # 2. 检查常见幻觉词库的大量重复（中英文/短语）
        # 比如：yeahyeahyeah, thank you for watching, 谢谢大家, 订阅, subscribe 等
        hallucination_triggers = ["yeah", "thank", "watching", "subscribe", "谢谢", "大家", "感谢", "收看", "订阅"]
        for trigger in hallucination_triggers:
            if text_lower.count(trigger) > 4:
                return ""
                
        return text

    def _to_simplified(self, text):
        try:
            import opencc
            return opencc.OpenCC("t2s").convert(text)
        except Exception as e:
            self.root.after(0, self._log, f"\n⚠️ 繁简转换失败：{e}\n")
            return text

    def _translate(self, text, target):
        try:
            from deep_translator import GoogleTranslator
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            return "\n".join(
                GoogleTranslator(source="auto", target=target).translate(c)
                for c in chunks)
        except Exception as e:
            self.root.after(0, self._log, f"\n⚠️ 翻译失败：{e}\n")
            return None

    def _log(self, text):
        self.log_txt.insert(tk.END, text)
        self.log_txt.see(tk.END)

    def _show(self, widget, text, tab_idx):
        widget.insert(tk.END, text)
        self.nb.select(tab_idx)

    def _status(self, msg):
        self.status_lbl.config(text=msg)

    def _done(self):
        self.progress.stop()
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)


if __name__ == "__main__":
    root = tk.Tk()
    WhisperApp(root)
    root.mainloop()