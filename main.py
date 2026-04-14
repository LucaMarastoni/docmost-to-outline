#!/usr/bin/env python3
"""
Prepare a Docmost export ZIP for Outline import as a SINGLE ZIP
using a hybrid Outline-like folder structure.

v12:
- imports ONLY markdown documents
- converts BMP files to PNG
- creates one ZIP with a top-level collection folder
- preserves the ORIGINAL parent folder hierarchy of the markdown files
- for each markdown doc, creates a document folder at its original location
- copies referenced local assets into:
    <doc-folder>/uploads/imported-user/<asset-id>/<filename>
- rewrites markdown asset refs to relative paths inside the document folder
- preserves external URLs
- skips oversized markdown docs

Example:
    python docmost_to_outline_prepare_v12.py "Graphimecc Wiki-export.markdown.zip"
    python docmost_to_outline_prepare_v12.py "Graphimecc Wiki-export.markdown.zip" --collection-name "Graphimecc Wiki-export.markdown"
    python docmost_to_outline_prepare_v12.py "Graphimecc Wiki-export.markdown.zip" --output-zip "Graphimecc-outline-like.zip"

Dependency:
    pip install pillow
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import posixpath
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from PIL import Image
except ImportError as exc:
    raise SystemExit("Missing dependency: Pillow. Install it with: pip install pillow") from exc


TEXT_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd"}
SKIP_URL_PREFIXES = ("http://", "https://", "mailto:", "data:", "#", "javascript:")
MARKDOWN_LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^\)\n]+)(\))")
HTML_ATTR_RE = re.compile(r'((?:src|href)\s*=\s*["\'])([^"\']+)(["\'])', re.IGNORECASE)
DEFAULT_MAX_DOC_BYTES = 2 * 1024 * 1024  # 2 MB


class ProcessingError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Docmost export ZIP into one Outline-like Markdown ZIP."
    )
    parser.add_argument("input_zip", help="Path to the Docmost export ZIP.")
    parser.add_argument(
        "--output-zip",
        help="Output ZIP path. Default: <input-folder>/<input-stem>-outline-like.zip",
    )
    parser.add_argument(
        "--collection-name",
        help="Top-level collection folder name inside the ZIP. Default: input ZIP stem",
    )
    parser.add_argument(
        "--delete-bmp",
        action="store_true",
        help="Delete original BMP files after successful PNG conversion in temp workspace.",
    )
    parser.add_argument(
        "--max-doc-bytes",
        type=int,
        default=DEFAULT_MAX_DOC_BYTES,
        help=f"Skip markdown docs larger than this many bytes. Default: {DEFAULT_MAX_DOC_BYTES}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and report changes without writing the output ZIP.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed operations.",
    )
    return parser.parse_args()


def normalize_to_posix(value: str) -> str:
    return value.replace("\\", "/")


def path_str(path: Path) -> str:
    return str(path.resolve(strict=False))


def safe_relpath(path: Path, start: Path) -> str:
    return normalize_to_posix(os.path.relpath(path_str(path), path_str(start)))


def is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath([os.path.normcase(path_str(path)), os.path.normcase(path_str(root))])
        return common == os.path.normcase(path_str(root))
    except ValueError:
        return False


def ensure_input_zip(path: Path) -> None:
    if not path.exists():
        raise ProcessingError(f"Input ZIP not found: {path}")
    if not path.is_file():
        raise ProcessingError(f"Input path is not a file: {path}")
    if path.suffix.lower() != ".zip":
        raise ProcessingError("Input file must be a .zip archive")


def default_output_zip(input_zip: Path) -> Path:
    return input_zip.with_name(f"{input_zip.stem}-outline-like.zip")


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        dest_root = destination.resolve()
        for member in archive.infolist():
            member_path = destination / member.filename
            resolved_destination = member_path.resolve()
            if not str(resolved_destination).startswith(str(dest_root)):
                raise ProcessingError(f"Unsafe ZIP entry detected: {member.filename}")
        archive.extractall(destination)


def sanitize_name(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip(" ._-") or "item"
    cleaned = cleaned.replace("/", "-").replace("\\", "-")
    return cleaned[:max_len]


def convert_bmp_to_png(bmp_path: Path, png_path: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(bmp_path) as image:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA") if "A" in image.getbands() else image.convert("RGB")
        image.save(png_path, format="PNG")


def collect_bmp_conversions(root: Path, delete_bmp: bool, verbose: bool) -> Dict[str, str]:
    conversions: Dict[str, str] = {}
    bmp_files = sorted(root.rglob("*.bmp")) + sorted(root.rglob("*.BMP"))
    seen: Set[Path] = set()

    for bmp_path in bmp_files:
        if bmp_path in seen:
            continue
        seen.add(bmp_path)
        rel_bmp = safe_relpath(bmp_path, root)
        png_path = bmp_path.with_suffix(".png")
        rel_png = safe_relpath(png_path, root)
        convert_bmp_to_png(bmp_path, png_path)
        conversions[rel_bmp] = rel_png
        if verbose:
            print(f"[convert] {rel_bmp} -> {rel_png}")
        if delete_bmp:
            bmp_path.unlink()
            if verbose:
                print(f"[delete]  {rel_bmp}")

    return conversions


def build_reference_candidates(rel_path: str) -> List[str]:
    posix_rel = normalize_to_posix(rel_path)
    candidates = {
        posix_rel,
        "./" + posix_rel,
        html.escape(posix_rel),
        html.escape("./" + posix_rel),
    }
    if " " in posix_rel:
        encoded = posix_rel.replace(" ", "%20")
        candidates.update({encoded, "./" + encoded, html.escape(encoded), html.escape("./" + encoded)})
    return sorted(candidates, key=len, reverse=True)


def replace_bmp_references(text: str, file_rel_posix: str, conversions: Dict[str, str]) -> Tuple[str, int]:
    replacements = 0
    file_dir = posixpath.dirname(file_rel_posix)
    for old_asset_rel, new_asset_rel in conversions.items():
        old_from_here = posixpath.relpath(old_asset_rel, start=file_dir or ".")
        new_from_here = posixpath.relpath(new_asset_rel, start=file_dir or ".")
        for old_candidate in build_reference_candidates(old_from_here):
            text, count = re.subn(re.escape(old_candidate), new_from_here.replace("\\", "/"), text)
            replacements += count
    return text, replacements


def update_markdown_bmp_refs(root: Path, conversions: Dict[str, str], verbose: bool) -> Tuple[int, List[str]]:
    touched: List[str] = []
    total = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        rel = safe_relpath(path, root)
        original = path.read_text(encoding="utf-8", errors="ignore")
        updated, replaced = replace_bmp_references(original, rel, conversions)
        if updated != original:
            path.write_text(updated, encoding="utf-8", newline="\n")
            touched.append(rel)
            total += replaced
            if verbose:
                print(f"[update]  {rel} (bmp refs: {replaced})")

    return total, touched


def find_markdown_documents(root: Path, max_doc_bytes: int, verbose: bool) -> Tuple[List[Path], List[Tuple[str, int]]]:
    docs: List[Path] = []
    skipped: List[Tuple[str, int]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rel = safe_relpath(path, root)
        if size > max_doc_bytes:
            skipped.append((rel, size))
            if verbose:
                print(f"[skip]    {rel} ({size} bytes > max {max_doc_bytes})")
            continue
        docs.append(path)

    return docs, skipped


def decode_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("<") and ref.endswith(">"):
        ref = ref[1:-1].strip()
    ref = html.unescape(ref)
    ref = ref.replace("%20", " ")
    return normalize_to_posix(ref)


def is_local_ref(ref: str) -> bool:
    lower = ref.lower().strip()
    return bool(lower) and not lower.startswith(SKIP_URL_PREFIXES)


def resolve_local_ref(root: Path, file_rel_posix: str, ref: str) -> Optional[Path]:
    cleaned = decode_ref(ref)
    if not is_local_ref(cleaned):
        return None
    file_dir = posixpath.dirname(file_rel_posix)
    joined = posixpath.normpath(posixpath.join(file_dir, cleaned)) if file_dir else posixpath.normpath(cleaned)
    candidate = (root / joined).resolve(strict=False)
    if not is_within(candidate, root):
        return None
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def asset_id_for_path(source: Path) -> str:
    return hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]


def source_doc_parent_rel(root: Path, doc_path: Path) -> str:
    rel_doc = safe_relpath(doc_path, root)
    parent = posixpath.dirname(rel_doc)
    return "" if parent in ("", ".") else parent


def source_doc_folder_name(doc_path: Path) -> str:
    return sanitize_name(doc_path.stem, 70) or "document"


def copy_and_rewrite_doc(
    root: Path,
    doc_path: Path,
    collection_dir: Path,
    verbose: bool,
) -> Tuple[int, int, int]:
    """
    Create:
      <collection>/<original-parent-folders>/<doc-stem>/<doc-stem>.md
      <collection>/<original-parent-folders>/<doc-stem>/uploads/imported-user/<asset-id>/<filename>

    and rewrite local asset refs accordingly.
    """
    rel_doc = safe_relpath(doc_path, root)
    original_text = doc_path.read_text(encoding="utf-8", errors="ignore")

    parent_rel = source_doc_parent_rel(root, doc_path)
    doc_folder = source_doc_folder_name(doc_path)

    if parent_rel:
        doc_dir = collection_dir / parent_rel / doc_folder
    else:
        doc_dir = collection_dir / doc_folder

    doc_dir.mkdir(parents=True, exist_ok=True)

    markdown_name = sanitize_name(doc_path.stem, 70) + ".md"
    markdown_out = doc_dir / markdown_name

    asset_map: Dict[Path, str] = {}
    copied_assets = 0
    rewritten_refs = 0
    unresolved_refs = 0

    def replace_match(match: re.Match) -> str:
        nonlocal copied_assets, rewritten_refs, unresolved_refs

        prefix, ref, suffix = match.groups()
        target = resolve_local_ref(root, rel_doc, ref)
        if not target:
            if is_local_ref(ref):
                unresolved_refs += 1
            return match.group(0)

        if target.suffix.lower() in TEXT_EXTENSIONS:
            return match.group(0)

        if target not in asset_map:
            asset_id = asset_id_for_path(target)
            asset_dir = doc_dir / "uploads" / "imported-user" / asset_id
            asset_dir.mkdir(parents=True, exist_ok=True)

            filename = sanitize_name(target.name, 120)
            asset_out = asset_dir / filename
            shutil.copy2(target, asset_out)

            rel_from_md = safe_relpath(asset_out, doc_dir)
            asset_map[target] = rel_from_md
            copied_assets += 1

            if verbose:
                print(f"[copy]    {safe_relpath(target, root)} -> {safe_relpath(asset_out, collection_dir)}")

        rewritten_refs += 1
        return f"{prefix}{asset_map[target]}{suffix}"

    updated_text = MARKDOWN_LINK_RE.sub(replace_match, original_text)
    updated_text = HTML_ATTR_RE.sub(replace_match, updated_text)

    markdown_out.write_text(updated_text, encoding="utf-8", newline="\n")

    return copied_assets, rewritten_refs, unresolved_refs


def make_zip_from_directory(source_dir: Path, output_zip: Path) -> None:
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, safe_relpath(path, source_dir))


def main() -> int:
    args = parse_args()
    input_zip = Path(args.input_zip).expanduser().resolve()
    output_zip = Path(args.output_zip).expanduser().resolve() if args.output_zip else default_output_zip(input_zip).resolve()

    try:
        ensure_input_zip(input_zip)

        default_collection = sanitize_name(input_zip.stem, 100) or "Imported Docmost"
        collection_name = sanitize_name(args.collection_name, 100) if args.collection_name else default_collection

        with tempfile.TemporaryDirectory(prefix="docmost_outline_v12_") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            extracted_dir = temp_dir / "extracted"
            build_dir = temp_dir / "build"
            extracted_dir.mkdir(parents=True, exist_ok=True)
            build_dir.mkdir(parents=True, exist_ok=True)

            safe_extract_zip(input_zip, extracted_dir)

            conversions = collect_bmp_conversions(extracted_dir, args.delete_bmp, args.verbose)
            bmp_replacements, touched = update_markdown_bmp_refs(extracted_dir, conversions, args.verbose)
            docs, skipped_large = find_markdown_documents(extracted_dir, args.max_doc_bytes, args.verbose)

            collection_dir = build_dir / collection_name
            collection_dir.mkdir(parents=True, exist_ok=True)

            total_assets = 0
            total_refs = 0
            total_unresolved = 0

            for doc in docs:
                copied_assets, rewritten_refs, unresolved_refs = copy_and_rewrite_doc(
                    extracted_dir,
                    doc,
                    collection_dir,
                    args.verbose,
                )
                total_assets += copied_assets
                total_refs += rewritten_refs
                total_unresolved += unresolved_refs

            print("Summary")
            print(f"- BMP converted: {len(conversions)}")
            print(f"- BMP path replacements: {bmp_replacements}")
            print(f"- Markdown files touched: {len(touched)}")
            print(f"- Markdown documents included: {len(docs)}")
            print(f"- Markdown documents skipped for size: {len(skipped_large)}")
            print(f"- Top-level collection: {collection_name}")
            print("- Strategy: one ZIP, original folder tree + per-document Outline-like folder")
            print(f"- Assets copied: {total_assets}")
            print(f"- Local references rewritten: {total_refs}")
            print(f"- Unresolved local refs: {total_unresolved}")
            print(f"- Original BMP files: {'deleted' if args.delete_bmp else 'kept'}")
            print(f"- Max markdown size: {args.max_doc_bytes} bytes")

            if skipped_large:
                print("- Skipped large markdown files:")
                for rel, size in skipped_large[:20]:
                    print(f"  - {rel} ({size} bytes)")
                if len(skipped_large) > 20:
                    print(f"  ... and {len(skipped_large) - 20} more")

            if args.dry_run:
                print(f"- Would create single ZIP: {output_zip}")
                print("Dry run: no output ZIP written.")
                return 0

            output_zip.parent.mkdir(parents=True, exist_ok=True)
            make_zip_from_directory(build_dir, output_zip)
            print(f"Single ZIP created: {output_zip}")
            return 0

    except ProcessingError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except zipfile.BadZipFile:
        print("Error: invalid or corrupted ZIP file.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())