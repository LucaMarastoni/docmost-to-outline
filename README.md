# Docmost to Outline Import Converter

Utility script to convert a Docmost markdown export ZIP into a format compatible with Outline import.

## Overview

This script processes a Docmost export archive and generates a single ZIP structured for Outline’s Markdown import system.

It performs the following operations:

- Extracts the input ZIP safely
- Detects and processes markdown documents
- Preserves the original folder hierarchy
- Wraps each document in an Outline-compatible directory structure
- Copies and normalizes local asset references (images, files)
- Converts BMP images to PNG (optional cleanup)
- Rewrites markdown and HTML asset links to valid relative paths
- Outputs a single ZIP ready for import into Outline

## Output Structure

The generated archive follows this pattern:

```
<collection>/
  <original-folders>/
    <document-name>/
      <document-name>.md
      uploads/
        imported-user/
          <asset-id>/
            <filename>
```

Each document is isolated in its own directory to match Outline’s expected import behavior. Assets are stored relative to their document and referenced via rewritten paths.

## Requirements

- Python 3.9+
- Pillow

Install dependencies:

```
pip install pillow
```

## Usage

Basic usage:

```
python3 docmost_to_outline_prepare_v12.py "input.zip"
```

Optional arguments:

```
--output-zip        Path to output ZIP file
--collection-name   Name of the root folder inside the archive
--delete-bmp        Remove original BMP files after conversion
--max-doc-bytes     Skip markdown files larger than this size (default: 2MB)
--dry-run           Analyze without generating output
--verbose           Enable detailed logging
```

Example:

```
python3 docmost_to_outline_prepare_v12.py "export.zip" \
  --collection-name "My Import" \
  --output-zip "outline-ready.zip"
```

## Notes

- Only local file references are rewritten; external URLs are preserved
- Markdown-to-markdown links are left unchanged
- Large markdown files exceeding the configured limit are skipped
- Asset paths are resolved relative to each document before rewriting
- The script is designed around observed Outline import behavior and is not officially supported by Outline

## Limitations

- Import success depends on Outline’s internal importer behavior, which may change
- Some edge-case references (non-standard markdown, malformed paths) may not resolve correctly
- Attachments are re-linked but not deduplicated across documents

## License

MIT (or specify your preferred license)
