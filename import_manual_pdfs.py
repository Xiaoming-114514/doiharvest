"""
Import manually supplemented PDFs from E:\工作\meta分析\99-198 into the pipeline.

For each PDF:
1. Extract article number from filename (e.g. "107Psychometric..." -> 107)
2. Look up DOI from the Excel reference file (row number = article number)
3. Get metadata (title, author, year, journal) from Crossref cache or API
4. Fall back to PDF text extraction if Crossref has no data
5. Generate filename: Author_Year_Journal_TitleWords.pdf
6. Copy PDF to output/papers/ directory
7. Update results_final CSV with proper metadata
"""

import json, os, re, csv, openpyxl, fitz, shutil, sys, time
import requests

# === Paths ===
EXCEL_PATH = r'C:\Users\16144\Documents\xwechat_files\wxid_ck5wd8fasg5g22_fc3b\msg\file\2026-07\新建 XLSX 工作表.xlsx'
PDF_FOLDER = r'E:\工作\meta分析\99-198'
PAPERS_DIR = r'C:\Users\16144\WorkBuddy\2026-07-28-00-26-53\doiharvest_oa\output\papers'
CSV_PATH = r'C:\Users\16144\WorkBuddy\2026-07-28-00-26-53\doiharvest_oa\output\results_final_20260729_085618.csv'
CACHE_PATH = r'C:\Users\16144\WorkBuddy\2026-07-28-00-26-53\doiharvest_oa\cache\metadata_cache.json'
PROJECT_DIR = r'C:\Users\16144\WorkBuddy\2026-07-28-00-26-53\doiharvest_oa'

sys.path.insert(0, PROJECT_DIR)
from backend.metadata import (
    _query_crossref, _extract_first_author, _extract_journal_abbrev,
    _extract_year, _extract_title, _sanitize_filename, _make_short_title,
    _load_cache, _save_cache
)


def extract_number(fname):
    """Extract leading number from filename."""
    m = re.match(r'^\s*(\d+)', fname)
    return int(m.group(1)) if m else None


def extract_metadata_from_pdf(pdf_path):
    """
    Extract title, first author, year, journal from PDF content.
    Used as fallback when Crossref has no data (e.g. Chinese papers).
    """
    doc = fitz.open(pdf_path)
    meta = doc.metadata

    # Try metadata first
    title = meta.get('title', '').strip() if meta.get('title') else ''
    author = meta.get('author', '').strip() if meta.get('author') else ''

    # Get first page text
    page0 = doc[0]
    text = page0.get_text()

    # Clean up title from metadata (remove encoding artifacts)
    if title:
        title = title.replace('â\x80\x93', '-').replace('â\x80\x94', '-')
        title = re.sub(r'[^\x00-\xFFFF]', '', title).strip()

    # If no title from metadata, try to extract from first page
    if not title or len(title) < 10:
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        # For Chinese papers, title is usually in the first few lines
        # For English papers, title is usually the first substantial line
        for line in lines[:10]:
            # Skip DOIs, URLs, journal headers
            if re.match(r'^https?://', line) or re.match(r'^doi:', line, re.I):
                continue
            if re.match(r'^\d{4}\s', line) or re.match(r'^Vol\.|^[A-Z][a-z]+\s+\d+', line):
                continue
            # Title should be reasonably long
            if len(line) > 15 and not line.endswith('.'):
                title = line
                break
            elif len(line) > 20:
                title = line
                break

    # Extract author from metadata or text
    if not author or author == 'CNKI':
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        for line in lines[:15]:
            # English author pattern: "Firstname Lastname" or "Lastname, Firstname"
            if re.match(r'^[A-Z][a-z]+\s+[A-Z]', line) and len(line) < 100:
                # Take the first author surname
                parts = line.split()
                if len(parts) >= 2:
                    author = parts[-1]  # Usually last word is surname for first author
                    break
            # Chinese author: usually a short line after the title
            if re.match(r'^[\u4e00-\u9fff]{2,4}$', line):
                author = line
                break

    # Extract first author surname
    first_author = 'Unknown'
    if author:
        if re.match(r'^[\u4e00-\u9fff]', author):
            # Chinese name - use full name (2-3 chars)
            first_author = author[:3]
        else:
            # English - take surname (last word, or first word if "Lastname, Firstname")
            if ',' in author:
                first_author = author.split(',')[0].strip()
            else:
                parts = author.split()
                # Skip initials, take the longest word as surname
                words = [w for w in parts if len(w) > 1 and not w.endswith('.')]
                if words:
                    first_author = words[0]

    # Extract year
    year = 'Unknown'
    year_match = re.search(r'\b(19[89]\d|20[0-2]\d)\b', text[:2000])
    if year_match:
        year = year_match.group(1)

    # Extract journal from first page text
    journal = 'UnknownJournal'
    # Look for common journal patterns
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for line in lines[:20]:
        # "Journal Name (Year) Vol:Page" pattern
        if re.search(r'\(\d{4}\)\s*\d+:', line):
            journal = re.sub(r'\s*\(\d{4}\).*', '', line).strip()
            break
        # "Journal Name, Volume(Issue)" pattern
        if re.search(r'\d+\(\d+\)', line) and len(line) < 100:
            journal = re.sub(r',?\s*\d+\(.*', '', line).strip()
            break
        # DOI line might contain journal info
        if line.startswith('https://doi.org/'):
            continue

    # Abbreviate journal name
    if journal and journal != 'UnknownJournal':
        words = [w for w in re.split(r'[^a-zA-Z0-9]+', journal) if w]
        journal = ''.join(words[:3])[:20]

    doc.close()

    return {
        'title': title or 'Untitled',
        'first_author': first_author,
        'year': year,
        'journal': journal,
    }


def get_metadata_for_doi(doi, cache):
    """Get metadata from cache or query Crossref."""
    doi_lower = doi.lower().strip()

    if doi_lower in cache and cache[doi_lower] is not None:
        return cache[doi_lower]

    # Query Crossref
    msg = _query_crossref(doi)
    if msg:
        title = _extract_title(msg)
        first_author = _extract_first_author(msg)
        year = _extract_year(msg)
        journal = _extract_journal_abbrev(msg)
        short_title = _make_short_title(title)

        safe_author = _sanitize_filename(first_author)
        safe_journal = _sanitize_filename(journal)
        safe_short = _sanitize_filename(short_title)

        filename = f"{safe_author}_{year}_{safe_journal}_{safe_short}.pdf"

        meta = {
            'doi': doi_lower,
            'title': title,
            'first_author': first_author,
            'year': year,
            'journal': journal,
            'filename': filename,
        }
        cache[doi_lower] = meta
        return meta

    cache[doi_lower] = None
    return None


def main():
    # === 1. Load Excel DOIs ===
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb.active
    excel_dois = {}
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=198, values_only=True)):
        doi = str(row[0]).split()[0] if row[0] else ''
        excel_dois[i + 1] = doi.lower().strip()

    # === 2. Load metadata cache ===
    cache = _load_cache()

    # === 3. List PDFs and extract article numbers ===
    pdfs = sorted(os.listdir(PDF_FOLDER))
    pdf_map = {}  # article_num -> (filepath, filename)
    for fname in pdfs:
        num = extract_number(fname)
        if num:
            pdf_map[num] = (os.path.join(PDF_FOLDER, fname), fname)

    print(f"Found {len(pdf_map)} PDFs to process")

    # === 4. Process each PDF ===
    results = []  # List of CSV row dicts
    copied = 0
    failed = 0
    pdf_fallback = 0

    for num in sorted(pdf_map.keys()):
        src_path, src_fname = pdf_map[num]
        doi = excel_dois.get(num, '')

        if not doi:
            print(f"  [SKIP] Row {num}: No DOI found in Excel")
            failed += 1
            continue

        # Get metadata
        meta = get_metadata_for_doi(doi, cache)

        if meta is None:
            # Fall back to PDF extraction
            print(f"  [PDF-FALLBACK] Row {num}: {doi} - extracting from PDF content")
            pdf_meta = extract_metadata_from_pdf(src_path)
            meta = {
                'doi': doi,
                'title': pdf_meta['title'],
                'first_author': pdf_meta['first_author'],
                'year': pdf_meta['year'],
                'journal': pdf_meta['journal'],
            }
            # Generate filename
            safe_author = _sanitize_filename(meta['first_author'])
            safe_journal = _sanitize_filename(meta['journal'])
            safe_short = _sanitize_filename(_make_short_title(meta['title']))
            meta['filename'] = f"{safe_author}_{meta['year']}_{safe_journal}_{safe_short}.pdf"
            pdf_fallback += 1

        # Copy PDF to papers directory
        target_filename = meta['filename']
        target_path = os.path.join(PAPERS_DIR, target_filename)

        # Handle filename conflicts
        if os.path.exists(target_path):
            # Check if it's the same file (already copied)
            src_size = os.path.getsize(src_path)
            tgt_size = os.path.getsize(target_path)
            if src_size == tgt_size:
                print(f"  [EXISTS] Row {num}: {target_filename} (already in papers)")
            else:
                # Add suffix to avoid conflict
                base, ext = os.path.splitext(target_filename)
                counter = 2
                while os.path.exists(target_path):
                    target_filename = f"{base}_{counter}{ext}"
                    target_path = os.path.join(PAPERS_DIR, target_filename)
                    counter += 1
                shutil.copy2(src_path, target_path)
                print(f"  [COPY] Row {num}: -> {target_filename}")
                copied += 1
        else:
            shutil.copy2(src_path, target_path)
            print(f"  [COPY] Row {num}: -> {target_filename}")
            copied += 1

        # Prepare CSV row
        row = {
            'doi': doi,
            'title': meta.get('title', ''),
            'first_author': meta.get('first_author', ''),
            'year': meta.get('year', ''),
            'journal': meta.get('journal', ''),
            'filename': target_filename,
            'status': 'downloaded',
            'phase': '1',
            'filepath': target_path,
            'message': f'manual_import_row{num}',
        }
        results.append(row)

    # Save updated cache
    _save_cache(cache)
    print(f"\nCache saved.")

    # === 5. Update CSV ===
    # Read existing CSV
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        existing_rows = list(reader)

    print(f"Existing CSV rows: {len(existing_rows)}")

    # Build a DOI -> row index map for existing rows
    doi_to_row = {}
    for i, row in enumerate(existing_rows):
        doi_key = row['doi'].lower().strip()
        doi_to_row[doi_key] = i

    # Update or append
    updated = 0
    appended = 0
    for new_row in results:
        doi_key = new_row['doi'].lower().strip()
        if doi_key in doi_to_row:
            # Update existing row
            idx = doi_to_row[doi_key]
            for key in fieldnames:
                if key in new_row and new_row[key]:
                    existing_rows[idx][key] = new_row[key]
            updated += 1
        else:
            # Append new row
            existing_rows.append(new_row)
            appended += 1

    # Write updated CSV
    with open(CSV_PATH, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing_rows)

    print(f"\n=== SUMMARY ===")
    print(f"PDFs processed: {len(pdf_map)}")
    print(f"PDFs copied to papers: {copied}")
    print(f"PDFs already in papers: {len(pdf_map) - copied - failed}")
    print(f"PDF fallback (no Crossref): {pdf_fallback}")
    print(f"CSV rows updated: {updated}")
    print(f"CSV rows appended: {appended}")
    print(f"Failed: {failed}")
    print(f"Total CSV rows: {len(existing_rows)}")


if __name__ == '__main__':
    main()
