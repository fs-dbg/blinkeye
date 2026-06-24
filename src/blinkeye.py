import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from PIL import Image, ImageTk
from PIL.ExifTags import TAGS

DEBUG = True


def debug(*args):
    if DEBUG:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}]", *args)


MAX_RECENT = 3
SIDEBAR_WIDTH = 150
THUMB_SIZE = (96, 96)

MIN_ZOOM = 0.05
MAX_ZOOM = 12.0
ZOOM_STEP = 1.15

SUPPORTED_EXTS = (
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp", ".ico"
)

BG_COLOR = "#1e1e1e"
SIDEBAR_COLOR = "#252526"
TAB_COLOR = "#2d2d2d"
TAB_ACTIVE_COLOR = "#3a76d8"
TAB_HOVER_COLOR = "#3c3c3c"
TEXT_COLOR = "#e0e0e0"
ACCENT_COLOR = "#3a76d8"
PIN_COLOR = "#d8a13a"
SECTION_LABEL_COLOR = "#888888"
DRAG_INDICATOR_COLOR = "#3a76d8"


class ImageEntry:
    """Holds data about one opened image."""

    _counter = 0

    def __init__(self, path):
        self.path = path
        self.display_name = os.path.basename(path)
        self.full_image = None       # Lazy loaded on tab switch
        self.thumbnail = None        # sidebar preview, made once via make_thumbnail()
        self.pinned = False
        self.imported_at = time.time()
        ImageEntry._counter += 1
        self.import_order = ImageEntry._counter   # used to sort recency, separate from view order
        debug(f"ImageEntry created: {self.display_name} (order={self.import_order})")

        # everything below is "how this image looks right now", kept per-entry
        # so flipping between tabs restores wherever you left each one
        self.zoom = None          # None until we know the canvas size to fit against
        self.offset_x = 0.0       # pan distance from dead center, in canvas pixels
        self.offset_y = 0.0
        self.manual_zoom = False  # flips true the moment the user scrolls or drags, see _fit_to_window
        self.display_photo = None  # the actual resampled image tk is showing right now
        self.display_size = None   # lets us skip re-resampling if nothing actually changed

    def load(self):
        if self.full_image is None:
            debug(f"Loading image from disk: {self.path}")
            img = Image.open(self.path)
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            self.full_image = img
            debug(f"Loaded {self.display_name}: size={img.size}, mode={img.mode}")
        return self.full_image

    def make_thumbnail(self):
        img = self.load().copy()
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        canvas = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
        offset = (
            (THUMB_SIZE[0] - img.width) // 2,
            (THUMB_SIZE[1] - img.height) // 2,
        )
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        canvas.paste(img, offset, img)
        self.thumbnail = ImageTk.PhotoImage(canvas)
        return self.thumbnail

    def get_exif(self):
        """Returns a dict of human-readable EXIF tags, or empty dict if none."""
        result = {}
        try:
            img = self.load()
            raw_exif = img.getexif()
            if not raw_exif:
                debug(f"No EXIF data found for {self.display_name}")
                return result
            for tag_id, value in raw_exif.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                if isinstance(value, bytes):
                    try:
                        value = value.decode(errors="replace")
                    except Exception:
                        value = repr(value)
                result[tag_name] = value
            debug(f"EXIF read for {self.display_name}: {len(result)} tags")
        except Exception as exc:
            debug(f"EXIF read failed for {self.display_name}: {exc}")
        return result


class ImageViewerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BlinkEye")
        self.geometry("1000x660")
        self.minsize(560, 380)
        self.configure(bg=BG_COLOR)

        self.entries = []          # list[ImageEntry], all opened images
        self.current_entry = None
        self.tab_rows = []         # list of (frame, entry) currently displayed, in display order

        self._drag_data = {"entry": None, "start_section": None}
        self._resize_job = None
        self._zoom_settle_job = None
        self._canvas_image_id = None

        self._build_ui()
        self._render_empty_state()

        # Bind to canvas <Configure> to prevent image clipping caused by race 
        # conditions during root window geometry updates.
        self._last_canvas_size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        self.canvas.bind("<Configure>", self._on_resize)
        debug("BlinkEye started")

    # ---------- UI construction ----------

    def _build_ui(self):
        self.sidebar = tk.Frame(self, bg=SIDEBAR_COLOR, width=SIDEBAR_WIDTH)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.tabs_scroll_canvas = tk.Canvas(
            self.sidebar, bg=SIDEBAR_COLOR, highlightthickness=0
        )
        self.tabs_scroll_canvas.pack(side="top", fill="both", expand=True)

        self.tabs_container = tk.Frame(self.tabs_scroll_canvas, bg=SIDEBAR_COLOR)
        self._tabs_window = self.tabs_scroll_canvas.create_window(
            (0, 0), window=self.tabs_container, anchor="nw"
        )

        def _sync_scroll_region(_event=None):
            self.tabs_scroll_canvas.configure(
                scrollregion=self.tabs_scroll_canvas.bbox("all")
            )

        def _sync_width(_event=None):
            self.tabs_scroll_canvas.itemconfig(
                self._tabs_window, width=self.tabs_scroll_canvas.winfo_width()
            )

        self.tabs_container.bind("<Configure>", _sync_scroll_region)
        self.tabs_scroll_canvas.bind("<Configure>", _sync_width)
        self.tabs_scroll_canvas.bind(
            "<MouseWheel>",
            lambda e: self.tabs_scroll_canvas.yview_scroll(int(-e.delta / 40), "units"),
        )

        self.open_btn = tk.Button(
            self.sidebar, text="+ Open Image", command=self.open_image,
            bg=ACCENT_COLOR, fg="white", activebackground="#2f63b8",
            activeforeground="white", relief="flat", font=("Segoe UI", 10),
            cursor="hand2", padx=8, pady=10, bd=0
        )
        self.open_btn.pack(fill="x", padx=10, pady=14, side="bottom")

        self.main_area = tk.Frame(self, bg=BG_COLOR)
        self.main_area.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(self.main_area, bg=BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="No image open")
        self.status_bar = tk.Label(
            self, textvariable=self.status_var, bg="#181818", fg="#999999",
            anchor="w", font=("Segoe UI", 9), padx=10, pady=4,
            width=1,  # stops long status text from fighting the canvas for width
        )
        self.status_bar.pack(side="bottom", fill="x")

        self.canvas.bind("<Double-Button-1>", lambda e: self.open_image())
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_release)

    def _render_empty_state(self):
        self.canvas.delete("all")
        self._canvas_image_id = None
        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 600
        self.canvas.create_text(
            w // 2, h // 2,
            text="No image open\n\nClick \"Open Image\" or double-click here to browse",
            fill="#666666", font=("Segoe UI", 12), justify="center"
        )

    # ---------- Opening / managing images ----------

    def open_image(self):
        path = filedialog.askopenfilename(
            title="Select an image",
            filetypes=[
                ("Image files", " ".join(f"*{e}" for e in SUPPORTED_EXTS)),
                ("All files", "*.*"),
            ],
        )
        if not path:
            debug("Open dialog cancelled")
            return
        debug(f"File selected: {path}")
        self._import_image(path)

    def _import_image(self, path):
        existing = next((e for e in self.entries if e.path == path), None)
        if existing:
            debug(f"Image already open, switching to it: {existing.display_name}")
            self._rebuild_tabs()
            self.update_idletasks()
            self._set_current(existing)
            return

        entry = ImageEntry(path)
        try:
            entry.load()
            entry.make_thumbnail()
        except Exception as exc:
            debug(f"Failed to import {path}: {exc}")
            messagebox.showerror("Could not open image", f"{os.path.basename(path)}\n\n{exc}")
            return

        self.entries.append(entry)
        debug(f"Imported new image: {entry.display_name} (total entries: {len(self.entries)})")
        self._enforce_recent_limit()

        # rebuild tabs and flush pending layout before fitting the image.
        # used to do this the other way round and the fit would run against
        # whatever size tk happened to be mid-recalculating at that instant
        self._rebuild_tabs()
        self.update_idletasks()
        self._set_current(entry)

    def _enforce_recent_limit(self):
        """Keep only MAX_RECENT unpinned entries, dropping the oldest by import time."""
        unpinned = [e for e in self.entries if not e.pinned]
        unpinned.sort(key=lambda e: e.import_order, reverse=True)
        if len(unpinned) > MAX_RECENT:
            to_drop = unpinned[MAX_RECENT:]
            for e in to_drop:
                debug(f"Dropping oldest recent tab (limit={MAX_RECENT}): {e.display_name}")
                self.entries.remove(e)
                if self.current_entry is e:
                    self.current_entry = None

    def _set_current(self, entry):
        debug(f"Switching current image to: {entry.display_name}")
        self.current_entry = entry
        if entry.zoom is None or not entry.manual_zoom:
            self._fit_to_window(entry)
        self._draw_current_image()
        self._update_status()

    def _update_status(self):
        entry = self.current_entry
        if not entry:
            self.status_var.set("No image open")
            return
        try:
            w, h = entry.load().size
            size_kb = os.path.getsize(entry.path) / 1024
            zoom_pct = int(round((entry.zoom or 1.0) * 100))
            self.status_var.set(
                f"{entry.display_name}   |   {w}x{h}px   |   {size_kb:.0f} KB   |   {zoom_pct}%"
            )
        except Exception:
            self.status_var.set(entry.display_name)

    # ---------- Sidebar tabs ----------

    def _rebuild_tabs(self):
        for child in self.tabs_container.winfo_children():
            child.destroy()
        self.tab_rows = []

        pinned = sorted(
            [e for e in self.entries if e.pinned],
            key=lambda e: e.import_order,
        )
        recent = sorted(
            [e for e in self.entries if not e.pinned],
            key=lambda e: e.import_order, reverse=True,
        )

        if pinned:
            self._add_section_label("PINNED")
            for entry in pinned:
                self._add_tab_row(entry, "pinned")

        self._add_section_label("RECENT")
        if not recent:
            empty_lbl = tk.Label(
                self.tabs_container, text="No recent images", bg=SIDEBAR_COLOR,
                fg="#555555", font=("Segoe UI", 8), anchor="w"
            )
            empty_lbl.pack(fill="x", padx=12, pady=(0, 8))
        for entry in recent:
            self._add_tab_row(entry, "recent")

    def _add_section_label(self, text):
        lbl = tk.Label(
            self.tabs_container, text=text, bg=SIDEBAR_COLOR, fg=SECTION_LABEL_COLOR,
            font=("Segoe UI", 9, "bold"), anchor="w"
        )
        lbl.pack(fill="x", padx=12, pady=(14, 6))

    def _add_tab_row(self, entry, section):
        is_active = entry is self.current_entry
        frame = tk.Frame(
            self.tabs_container,
            bg=TAB_ACTIVE_COLOR if is_active else TAB_COLOR,
            cursor="hand2",
            padx=6, pady=6,
        )
        frame.pack(fill="x", padx=10, pady=4)

        top_row = tk.Frame(frame, bg=frame["bg"])
        top_row.pack(fill="x")

        thumb_lbl = tk.Label(top_row, image=entry.thumbnail, bg=frame["bg"])
        thumb_lbl.image = entry.thumbnail
        thumb_lbl.pack(side="left")

        if entry.pinned:
            pin_lbl = tk.Label(top_row, text="*", bg=frame["bg"], fg=PIN_COLOR,
                                font=("Segoe UI", 11, "bold"))
            pin_lbl.pack(side="right", anchor="ne")

        name_lbl = tk.Label(
            frame, text=self._truncate(entry.display_name), bg=frame["bg"],
            fg=TEXT_COLOR if is_active else "#bbbbbb",
            font=("Segoe UI", 8), wraplength=THUMB_SIZE[0]
        )
        name_lbl.pack(pady=(4, 0))

        widgets = [frame, top_row, thumb_lbl, name_lbl]
        for widget in widgets:
            widget.bind("<Button-1>", lambda e, en=entry: self._select_tab(en))
            widget.bind("<ButtonPress-1>", lambda e, en=entry, sec=section: self._on_drag_start(e, en, sec), add="+")
            widget.bind("<B1-Motion>", lambda e, en=entry: self._on_drag_motion(e, en))
            widget.bind("<ButtonRelease-1>", lambda e, en=entry: self._on_drag_release(e, en))
            widget.bind("<Button-3>", lambda e, en=entry: self._show_tab_menu(e, en))
            if not is_active:
                widget.bind("<Enter>", lambda e, f=frame: self._set_row_color(f, TAB_HOVER_COLOR))
                widget.bind("<Leave>", lambda e, f=frame: self._set_row_color(f, TAB_COLOR))

        self.tab_rows.append((frame, entry, section))

    @staticmethod
    def _set_row_color(frame, color):
        frame.configure(bg=color)
        for child in frame.winfo_children():
            try:
                child.configure(bg=color)
            except tk.TclError:
                pass
            for grandchild in child.winfo_children():
                try:
                    grandchild.configure(bg=color)
                except tk.TclError:
                    pass

    @staticmethod
    def _truncate(name, max_len=16):
        if len(name) <= max_len:
            return name
        keep = max_len - 3
        return name[: keep // 2] + "..." + name[-(keep - keep // 2):]

    def _select_tab(self, entry):
        if self._drag_data.get("dragged"):
            return
        if entry is self.current_entry:
            return
        debug(f"Tab clicked: {entry.display_name}")
        self._set_current(entry)
        self._rebuild_tabs()

    # ---------- Drag to reorder ----------

    def _on_drag_start(self, event, entry, section):
        self._drag_data = {
            "entry": entry, "start_section": section,
            "start_y": event.y_root, "dragged": False,
        }

    def _on_drag_motion(self, event, entry):
        if self._drag_data.get("entry") is not entry:
            return
        if abs(event.y_root - self._drag_data.get("start_y", event.y_root)) > 6:
            self._drag_data["dragged"] = True

    def _on_drag_release(self, event, entry):
        data = self._drag_data
        if not data.get("dragged") or data.get("entry") is not entry:
            self._drag_data = {"entry": None, "start_section": None}
            return

        section = data["start_section"]
        same_section_entries = [
            e for (_, e, sec) in self.tab_rows if sec == section
        ]
        if entry not in same_section_entries:
            self._drag_data = {"entry": None, "start_section": None}
            return

        target_y = event.y_root
        target_entry = entry
        best_dist = None
        for (frame, e, sec) in self.tab_rows:
            if sec != section:
                continue
            try:
                fy = frame.winfo_rooty() + frame.winfo_height() // 2
            except tk.TclError:
                continue
            dist = abs(fy - target_y)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                target_entry = e

        if target_entry is not entry:
            debug(f"Reordering: moved {entry.display_name} next to {target_entry.display_name} in '{section}'")
            self._reorder(entry, target_entry, section)

        self._drag_data = {"entry": None, "start_section": None}

    def _reorder(self, moved_entry, target_entry, section):
        """Reassigns import_order values so moved_entry sits where target_entry was,
        within its section, preserving relative order of the rest."""
        ordered = [
            e for (_, e, sec) in self.tab_rows if sec == section
        ]
        if moved_entry not in ordered or target_entry not in ordered:
            return
        ordered.remove(moved_entry)
        target_idx = ordered.index(target_entry)
        ordered.insert(target_idx, moved_entry)

        # Re-stamp import_order to reflect the new sequence.
        # "Recent" is displayed most-recent-first (descending import_order),
        # "Pinned" is displayed in ascending import_order (oldest pinned first).
        if section == "recent":
            base = max((e.import_order for e in self.entries), default=0) + len(ordered)
            for i, e in enumerate(ordered):
                e.import_order = base - i
        else:
            base = min((e.import_order for e in self.entries), default=0) - len(ordered)
            for i, e in enumerate(ordered):
                e.import_order = base + i

        self._rebuild_tabs()

    # ---------- Right-click tab menu ----------

    def _show_tab_menu(self, event, entry):
        menu = tk.Menu(self, tearoff=0, bg=TAB_COLOR, fg=TEXT_COLOR,
                        activebackground=ACCENT_COLOR, activeforeground="white",
                        relief="flat")
        if entry.pinned:
            menu.add_command(label="Unpin", command=lambda: self._toggle_pin(entry))
        else:
            menu.add_command(label="Pin", command=lambda: self._toggle_pin(entry))
        menu.add_separator()
        menu.add_command(label="Move Up", command=lambda: self._move(entry, -1))
        menu.add_command(label="Move Down", command=lambda: self._move(entry, 1))
        menu.add_separator()
        menu.add_command(label="Rename...", command=lambda: self._rename_entry(entry))
        menu.add_command(label="Properties / Metadata...", command=lambda: self._show_properties(entry))
        menu.add_separator()
        menu.add_command(label="Remove", command=lambda: self._remove_entry(entry))
        menu.tk_popup(event.x_root, event.y_root)

    def _toggle_pin(self, entry):
        entry.pinned = not entry.pinned
        debug(f"{'Pinned' if entry.pinned else 'Unpinned'}: {entry.display_name}")
        if entry.pinned:
            max_pinned = max((e.import_order for e in self.entries if e.pinned and e is not entry), default=0)
            entry.import_order = max_pinned + 1
        else:
            entry.import_order = ImageEntry._counter + 1
            ImageEntry._counter += 1
            self._enforce_recent_limit()
        self._rebuild_tabs()

    def _move(self, entry, direction):
        section = "pinned" if entry.pinned else "recent"
        ordered = [e for (_, e, sec) in self.tab_rows if sec == section]
        if entry not in ordered:
            return
        idx = ordered.index(entry)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(ordered):
            debug(f"Move {'up' if direction < 0 else 'down'} ignored, already at edge: {entry.display_name}")
            return
        ordered[idx], ordered[new_idx] = ordered[new_idx], ordered[idx]
        debug(f"Moved {entry.display_name} {'up' if direction < 0 else 'down'} in '{section}'")

        if section == "recent":
            base = max((e.import_order for e in self.entries), default=0) + len(ordered)
            for i, e in enumerate(ordered):
                e.import_order = base - i
        else:
            base = min((e.import_order for e in self.entries), default=0) - len(ordered)
            for i, e in enumerate(ordered):
                e.import_order = base + i

        self._rebuild_tabs()

    def _rename_entry(self, entry):
        new_name = simpledialog.askstring(
            "Rename", "Display name:", initialvalue=entry.display_name, parent=self
        )
        if new_name and new_name.strip():
            debug(f"Renamed '{entry.display_name}' to '{new_name.strip()}'")
            entry.display_name = new_name.strip()
            self._rebuild_tabs()
            if entry is self.current_entry:
                self._update_status()

    def _remove_entry(self, entry):
        debug(f"Removing tab: {entry.display_name}")
        if entry in self.entries:
            self.entries.remove(entry)
        self._rebuild_tabs()
        self.update_idletasks()
        if self.current_entry is entry:
            self.current_entry = self.entries[0] if self.entries else None
            if self.current_entry:
                self._set_current(self.current_entry)
            else:
                self._render_empty_state()
                self._update_status()

    def _show_properties(self, entry):
        debug(f"Opening properties for: {entry.display_name}")
        win = tk.Toplevel(self)
        win.title(f"Properties - {entry.display_name}")
        win.configure(bg=BG_COLOR)
        win.geometry("420x480")
        win.transient(self)

        header = tk.Label(
            win, text=entry.display_name, bg=BG_COLOR, fg=TEXT_COLOR,
            font=("Segoe UI", 11, "bold"), anchor="w", wraplength=380
        )
        header.pack(fill="x", padx=14, pady=(14, 6))

        try:
            img = entry.load()
            w, h = img.size
            fmt = img.format or "Unknown"
            mode = img.mode
        except Exception:
            w = h = 0
            fmt = "Unknown"
            mode = "Unknown"

        try:
            size_bytes = os.path.getsize(entry.path)
            size_kb = size_bytes / 1024
        except Exception:
            size_kb = 0

        basics = [
            ("File path", entry.path),
            ("Dimensions", f"{w} x {h} px"),
            ("Format", fmt),
            ("Color mode", mode),
            ("File size", f"{size_kb:.1f} KB"),
        ]

        basics_frame = tk.Frame(win, bg=BG_COLOR)
        basics_frame.pack(fill="x", padx=14, pady=(0, 8))
        for label, value in basics:
            row = tk.Frame(basics_frame, bg=BG_COLOR)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{label}:", bg=BG_COLOR, fg="#999999",
                     font=("Segoe UI", 9), width=12, anchor="w").pack(side="left")
            tk.Label(row, text=str(value), bg=BG_COLOR, fg=TEXT_COLOR,
                     font=("Segoe UI", 9), anchor="w", wraplength=270,
                     justify="left").pack(side="left", fill="x", expand=True)

        sep = tk.Frame(win, bg="#3a3a3a", height=1)
        sep.pack(fill="x", padx=14, pady=8)

        exif_lbl = tk.Label(win, text="EXIF metadata", bg=BG_COLOR, fg=TEXT_COLOR,
                             font=("Segoe UI", 10, "bold"), anchor="w")
        exif_lbl.pack(fill="x", padx=14, pady=(0, 6))

        exif_data = entry.get_exif()

        list_frame = tk.Frame(win, bg=BG_COLOR)
        list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        if not exif_data:
            tk.Label(
                list_frame, text="No EXIF metadata found in this file.",
                bg=BG_COLOR, fg="#777777", font=("Segoe UI", 9), anchor="w"
            ).pack(fill="x")
        else:
            tree_scroll = tk.Scrollbar(list_frame)
            tree_scroll.pack(side="right", fill="y")

            text_widget = tk.Text(
                list_frame, bg="#181818", fg=TEXT_COLOR, relief="flat",
                font=("Consolas", 9), wrap="word", yscrollcommand=tree_scroll.set
            )
            text_widget.pack(side="left", fill="both", expand=True)
            tree_scroll.configure(command=text_widget.yview)

            for key in sorted(exif_data.keys(), key=str):
                text_widget.insert("end", f"{key}: {exif_data[key]}\n")
            text_widget.configure(state="disabled")

        close_btn = tk.Button(
            win, text="Close", command=win.destroy,
            bg=ACCENT_COLOR, fg="white", relief="flat",
            font=("Segoe UI", 9), cursor="hand2", padx=10, pady=6, bd=0
        )
        close_btn.pack(pady=(0, 14))

    # ---------- Zoom and pan ----------

    def _fit_to_window(self, entry, retry=True):
        self.canvas.update_idletasks()
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            # Canvas has not been laid out yet (e.g. first image opened
            # before the window has done its first geometry pass). Defer
            # and retry shortly rather than computing against a bogus 1x1
            # size, which used to produce a tiny/garbage zoom value.
            if retry:
                debug(f"Canvas not ready yet ({canvas_w}x{canvas_h}), deferring fit for {entry.display_name}")
                self.after(30, lambda: self._fit_to_window_and_redraw(entry))
            return
        img = entry.load()
        margin = 24
        avail_w = max(canvas_w - margin * 2, 50)
        avail_h = max(canvas_h - margin * 2, 50)
        scale = min(avail_w / img.width, avail_h / img.height)
        entry.zoom = max(min(scale, MAX_ZOOM), MIN_ZOOM)
        entry.offset_x = 0.0
        entry.offset_y = 0.0
        debug(
            f"Fit to window: {entry.display_name} -> zoom={entry.zoom:.3f} "
            f"(canvas={canvas_w}x{canvas_h}, image={img.width}x{img.height})"
        )

    def _fit_to_window_and_redraw(self, entry):
        if not self.winfo_exists():
            return
        self._fit_to_window(entry)
        if entry is self.current_entry:
            self._draw_current_image(fast=False)

    def _on_mouse_wheel(self, event):
        entry = self.current_entry
        if not entry:
            return
        if entry.zoom is None:
            self._fit_to_window(entry)
        if entry.zoom is None:
            # Canvas still not laid out; nothing sensible to zoom yet.
            return

        old_zoom = entry.zoom
        factor = ZOOM_STEP if event.delta > 0 else (1 / ZOOM_STEP)
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, old_zoom * factor))
        if new_zoom == old_zoom:
            return

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        center_x = canvas_w / 2 + entry.offset_x
        center_y = canvas_h / 2 + entry.offset_y

        cursor_x = event.x
        cursor_y = event.y

        img_x = (cursor_x - center_x) / old_zoom
        img_y = (cursor_y - center_y) / old_zoom

        new_center_x = cursor_x - img_x * new_zoom
        new_center_y = cursor_y - img_y * new_zoom

        entry.offset_x = new_center_x - canvas_w / 2
        entry.offset_y = new_center_y - canvas_h / 2
        entry.zoom = new_zoom
        entry.manual_zoom = True
        debug(f"Zoom: {entry.display_name} -> {new_zoom:.3f}x at cursor ({cursor_x},{cursor_y})")

        # Fast path while the wheel is actively spinning: draw at low-cost
        # interpolation immediately, then schedule one high-quality redraw
        # a moment after scrolling stops. This is what makes zoom feel snappy.
        self._draw_current_image(fast=True)
        self._update_status()
        if self._zoom_settle_job:
            self.after_cancel(self._zoom_settle_job)
        self._zoom_settle_job = self.after(90, self._zoom_settle)

    def _zoom_settle(self):
        self._zoom_settle_job = None
        if not self.winfo_exists():
            return
        self._draw_current_image(fast=False)

    def _on_pan_start(self, event):
        if not self.current_entry:
            return
        debug(f"Pan start: {self.current_entry.display_name}")
        self._pan_data = {
            "x": event.x, "y": event.y,
            "offset_x": self.current_entry.offset_x,
            "offset_y": self.current_entry.offset_y,
        }

    def _on_pan_move(self, event):
        entry = self.current_entry
        if not entry or not hasattr(self, "_pan_data"):
            return
        dx = event.x - self._pan_data["x"]
        dy = event.y - self._pan_data["y"]
        entry.offset_x = self._pan_data["offset_x"] + dx
        entry.offset_y = self._pan_data["offset_y"] + dy
        entry.manual_zoom = True
        # Panning never needs to resample the image, just move the existing
        # PhotoImage on the canvas. Skip the expensive resize entirely.
        self._draw_current_image(fast=True, reuse_existing_photo=True)

    def _on_pan_release(self, event):
        if hasattr(self, "_pan_data"):
            del self._pan_data

    # ---------- Rendering ----------

    def _draw_current_image(self, fast=False, reuse_existing_photo=False):
        if not self.winfo_exists():
            return
        if not self.current_entry:
            self._render_empty_state()
            return

        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            self.after(50, self._draw_current_image)
            return

        entry = self.current_entry
        if entry.zoom is None or not entry.manual_zoom:
            # Always recompute the fit for images the user has not manually
            # zoomed/panned, using the canvas size as it is right now. This
            # is what prevents a stale zoom from an earlier (different)
            # canvas size from sticking around and cutting the image off.
            self._fit_to_window(entry)

        center_x = canvas_w // 2 + entry.offset_x
        center_y = canvas_h // 2 + entry.offset_y

        if reuse_existing_photo and entry.display_photo is not None:
            # Pure pan: just move the already-rendered image, no resample.
            self.canvas.coords(self._canvas_image_id, center_x, center_y)
            return

        img = entry.load()
        new_w = max(int(img.width * entry.zoom), 1)
        new_h = max(int(img.height * entry.zoom), 1)

        # Cap resampled size to something sane to avoid huge memory use at extreme zoom
        new_w = min(new_w, 8000)
        new_h = min(new_h, 8000)

        cache_key = (new_w, new_h, fast)
        if entry.display_size == cache_key and entry.display_photo is not None:
            resample_needed = False
        else:
            resample_needed = True

        if resample_needed:
            if fast:
                # Cheap, near-instant resample while the user is actively
                # interacting (scrolling). Looks slightly softer but costs
                # a fraction of LANCZOS time, which is what keeps zoom smooth.
                resample = Image.NEAREST if entry.zoom > 1 else Image.BILINEAR
            else:
                resample = Image.LANCZOS if entry.zoom <= 1 else Image.NEAREST
            resized = img.resize((new_w, new_h), resample)
            entry.display_photo = ImageTk.PhotoImage(resized)
            entry.display_size = cache_key

        if self._canvas_image_id is None or self.canvas.find_withtag(self._canvas_image_id) == ():
            self.canvas.delete("all")
            self._canvas_image_id = self.canvas.create_image(
                center_x, center_y, image=entry.display_photo, anchor="center"
            )
        else:
            self.canvas.itemconfig(self._canvas_image_id, image=entry.display_photo)
            self.canvas.coords(self._canvas_image_id, center_x, center_y)

    def _on_resize(self, event):
        # this fires on the canvas's own configure now, not the root window.
        # tk sends these for all sorts of internal reasons, some with the
        # same size as before, so skip the work if nothing actually changed.
        new_size = (event.width, event.height)
        if new_size == self._last_canvas_size:
            return
        self._last_canvas_size = new_size

        # reposition right away using whatever image we already have, so
        # the window doesn't flash empty while we wait on the real redraw
        self._reflow_for_resize()
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, self._handle_resize_done)

    def _reflow_for_resize(self):
        entry = self.current_entry
        if not entry:
            return
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return
        if not entry.manual_zoom:
            # Images the user hasn't manually zoomed should keep filling
            # the window as it changes shape, so just re-fit immediately.
            self._fit_to_window(entry)
        center_x = canvas_w // 2 + entry.offset_x
        center_y = canvas_h // 2 + entry.offset_y
        if self._canvas_image_id is not None and self.canvas.find_withtag(self._canvas_image_id) != ():
            self.canvas.coords(self._canvas_image_id, center_x, center_y)

    def _handle_resize_done(self):
        if not self.winfo_exists():
            return
        debug(f"Window resized to {self.winfo_width()}x{self.winfo_height()}")
        self._draw_current_image(fast=False)


def main():
    app = ImageViewerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
