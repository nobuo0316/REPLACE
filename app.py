import streamlit as st
from pathlib import Path
import zipfile
import tempfile
import difflib
import re
import io
import json
from datetime import datetime

st.set_page_config(page_title="Code Patch Tool", layout="wide")

st.title("🔧 Code Patch Tool (Streamlit)")
st.caption("Upload a ZIP project, select a file, apply partial fixes (replace/snippet/diff), preview changes, and download.")

# -----------------------------
# Helpers
# -----------------------------
TEXT_EXT = {
    ".py", ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".csv", ".sql", ".env"
}

def is_text_file(p: Path) -> bool:
    return p.suffix.lower() in TEXT_EXT

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_text(p: Path, content: str):
    p.write_text(content, encoding="utf-8")

def unified_diff(old: str, new: str, filename: str = "file") -> str:
    old_lines = old.splitlines(True)
    new_lines = new.splitlines(True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}",
        lineterm=""
    )
    return "\n".join(diff)

def apply_search_replace(text: str, find: str, repl: str, use_regex: bool) -> tuple[str, int]:
    if not find:
        return text, 0
    if use_regex:
        new_text, n = re.subn(find, repl, text, flags=re.MULTILINE)
        return new_text, n
    else:
        n = text.count(find)
        return text.replace(find, repl), n

def apply_snippet_replace(text: str, old_snip: str, new_snip: str) -> tuple[str, bool]:
    """
    Try exact snippet replacement first.
    If not found, try a fuzzy match using difflib on lines (best effort).
    """
    if not old_snip:
        return text, False

    if old_snip in text:
        return text.replace(old_snip, new_snip), True

    # Fuzzy: find most similar window in text lines
    text_lines = text.splitlines()
    old_lines = old_snip.splitlines()
    if len(old_lines) < 2 or len(text_lines) < len(old_lines):
        return text, False

    best_ratio = 0.0
    best_i = None
    for i in range(0, len(text_lines) - len(old_lines) + 1):
        window = "\n".join(text_lines[i:i+len(old_lines)])
        ratio = difflib.SequenceMatcher(None, window, "\n".join(old_lines)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_i = i

    # threshold (tune if needed)
    if best_i is not None and best_ratio >= 0.75:
        before = "\n".join(text_lines[:best_i])
        after = "\n".join(text_lines[best_i+len(old_lines):])
        rebuilt = (before + "\n" if before else "") + new_snip + ("\n" + after if after else "")
        return rebuilt, True

    return text, False

def zip_dir_to_bytes(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(root).as_posix())
    buf.seek(0)
    return buf.read()

# -----------------------------
# Session State
# -----------------------------
if "workdir" not in st.session_state:
    st.session_state.workdir = None
if "changes" not in st.session_state:
    st.session_state.changes = []  # change log

# -----------------------------
# Upload ZIP / File
# -----------------------------
left, right = st.columns([1, 2], gap="large")

with left:
    st.subheader("1) Upload")
    zip_up = st.file_uploader("Upload project ZIP", type=["zip"])
    single_up = st.file_uploader("Or upload a single file", type=None)

    if st.button("Reset workspace"):
        if st.session_state.workdir:
            try:
                st.session_state.workdir.cleanup()
            except Exception:
                pass
        st.session_state.workdir = None
        st.session_state.changes = []
        st.rerun()

    if zip_up:
        if st.session_state.workdir:
            try:
                st.session_state.workdir.cleanup()
            except Exception:
                pass
        tmp = tempfile.TemporaryDirectory()
        st.session_state.workdir = tmp
        root = Path(tmp.name)

        with zipfile.ZipFile(zip_up, "r") as z:
            z.extractall(root)

        st.success("ZIP extracted.")
        st.session_state.changes = []
        st.rerun()

    if single_up:
        if st.session_state.workdir:
            try:
                st.session_state.workdir.cleanup()
            except Exception:
                pass
        tmp = tempfile.TemporaryDirectory()
        st.session_state.workdir = tmp
        root = Path(tmp.name)
        # Save file to root
        target = root / single_up.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(single_up.read())
        st.success("File saved.")
        st.session_state.changes = []
        st.rerun()

# -----------------------------
# Main editor
# -----------------------------
with right:
    st.subheader("2) Select & Patch")

    if not st.session_state.workdir:
        st.info("Upload a ZIP or a file to start.")
        st.stop()

    root = Path(st.session_state.workdir.name)
    all_files = [p for p in root.rglob("*") if p.is_file() and is_text_file(p)]
    if not all_files:
        st.warning("No editable text files found in the uploaded content.")
        st.stop()

    rel_paths = [p.relative_to(root).as_posix() for p in all_files]
    rel_paths.sort()

    selected = st.selectbox("Select a file", rel_paths)
    file_path = root / selected
    original_text = read_text(file_path)

    st.write("**Current file:**", selected)

    # Show file content
    colA, colB = st.columns(2, gap="large")
    with colA:
        st.markdown("#### Current content (read-only)")
        st.text_area("",
                     value=original_text,
                     height=420,
                     key="current_view")

    with colB:
        st.markdown("#### Patch settings")
        mode = st.radio(
            "Patch mode",
            ["Search & Replace", "Snippet Replace (with fuzzy)", "Unified Diff (paste only - preview)", "Manual Edit (paste full content)"],
            index=0
        )

        new_text = original_text
        applied = False
        meta = {}

        if mode == "Search & Replace":
            find = st.text_area("Find", height=120)
            repl = st.text_area("Replace with", height=120)
            use_regex = st.checkbox("Use Regex", value=False)
            if st.button("Preview change"):
                new_text, n = apply_search_replace(original_text, find, repl, use_regex)
                applied = (n > 0 and new_text != original_text)
                meta = {"mode": mode, "matches": int(n), "regex": use_regex}

        elif mode == "Snippet Replace (with fuzzy)":
            old_snip = st.text_area("Old snippet (from AI or from file)", height=140)
            new_snip = st.text_area("New snippet", height=140)
            if st.button("Preview change"):
                new_text, ok = apply_snippet_replace(original_text, old_snip, new_snip)
                applied = (ok and new_text != original_text)
                meta = {"mode": mode, "fuzzy_or_exact": bool(ok)}

        elif mode == "Unified Diff (paste only - preview)":
            st.warning("This mode does not auto-apply diff in this minimal version. It shows where changes should be.")
            diff_text = st.text_area("Paste unified diff here", height=300)
            if st.button("Preview diff"):
                # Just show pasted diff; user can switch to Snippet Replace or Manual Edit to apply.
                meta = {"mode": mode, "note": "preview only"}
                applied = False
                new_text = original_text
                st.code(diff_text, language="diff")

        elif mode == "Manual Edit (paste full content)":
            edited_full = st.text_area("Paste the FULL updated file content", height=300, value=original_text)
            if st.button("Preview change"):
                new_text = edited_full
                applied = (new_text != original_text)
                meta = {"mode": mode}

    # Diff preview + Apply
    st.markdown("### 3) Diff Preview")
    diff_out = unified_diff(original_text, new_text, filename=selected)
    if diff_out.strip():
        st.code(diff_out, language="diff")
    else:
        st.caption("No changes to preview.")

    c1, c2, c3 = st.columns([1, 1, 2], gap="large")
    with c1:
        if st.button("Apply to file", disabled=(new_text == original_text)):
            write_text(file_path, new_text)
            st.session_state.changes.append({
                "time": datetime.utcnow().isoformat() + "Z",
                "file": selected,
                "meta": meta,
                "diff": diff_out[:20000],  # avoid huge logs
            })
            st.success("Applied.")
            st.rerun()

    with c2:
        if st.button("Revert file (reload from disk)"):
            st.rerun()

    with c3:
        st.markdown("### 4) Download")
        zip_bytes = zip_dir_to_bytes(root)
        st.download_button(
            "Download patched ZIP",
            data=zip_bytes,
            file_name="patched_project.zip",
            mime="application/zip"
        )

        st.download_button(
            "Download change log (JSON)",
            data=json.dumps(st.session_state.changes, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="patch_log.json",
            mime="application/json"
        )
``
