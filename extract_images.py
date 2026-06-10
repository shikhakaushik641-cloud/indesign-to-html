"""
extract_images.py
-----------------
Extracts images from an InDesign IDML+PDF pair and saves them as PNGs
that can be uploaded to the Content Pipeline via the folder-upload button.

Usage:
    python extract_images.py <path_to.idml> <path_to.pdf> [output_folder]

Outputs a folder of PNG files named by their original linked filename
(e.g. FC_JEE_XII_Chem_S.S_Dia01.eps.png, Chem.1_1.wmf.png, etc.)
The pipeline's findImage() does stem-matching so extensions are ignored.
"""

import fitz
import zipfile
import re
import os
import sys
import json
from pathlib import Path
from urllib.parse import unquote

# ─── Configuration ────────────────────────────────────────────────────────────
RENDER_SCALE   = 3.0   # render at 3× (216 dpi) for good quality
MIN_IMG_W      = 30    # minimum image region width in pts to consider
MIN_IMG_H      = 25    # minimum image region height in pts to consider
CLUSTER_GAP    = 8     # max gap in pts between drawings to cluster together
REPEAT_SKIP_PX = 90    # skip repeated identical-size regions (headers/footers)
# ─────────────────────────────────────────────────────────────────────────────


def parse_idml_images(idml_path):
    """
    Return an ordered list of linked image filenames from the IDML,
    in document reading order (story list order × appearance order within each story).
    Also returns a set of ALL linked filenames for reference.
    """
    ordered = []
    all_files = set()

    with zipfile.ZipFile(idml_path) as z:
        # Get story list order from designmap.xml
        dm_xml = z.read('designmap.xml').decode('utf-8')
        story_order_m = re.search(r'StoryList="([^"]+)"', dm_xml)
        story_id_order = story_order_m.group(1).split() if story_order_m else []

        # Build map: story_id → filename list
        story_files = {n for n in z.namelist() if n.startswith('Stories/')}

        def story_id_from_path(path):
            m = re.search(r'Story_([^.]+)\.xml$', path)
            return m.group(1) if m else None

        story_map = {}
        for sf in story_files:
            sid = story_id_from_path(sf)
            if not sid:
                continue
            xml = z.read(sf).decode('utf-8')
            # Find all LinkResourceURI in document order
            links = re.findall(r'LinkResourceURI="([^"]+)"', xml)
            fnames = []
            for uri in links:
                fname = unquote(uri.split('/')[-1].split('\\')[-1])
                if fname:
                    fnames.append(fname)
                    all_files.add(fname)
            story_map[sid] = fnames

        # Walk stories in document order
        for sid in story_id_order:
            for fname in story_map.get(sid, []):
                ordered.append(fname)

        # Append any story images not in StoryList (edge case)
        seen = set(ordered)
        for sid, fnames in story_map.items():
            if sid not in story_id_order:
                for fname in fnames:
                    if fname not in seen:
                        ordered.append(fname)
                        seen.add(fname)

        # Also pick up spread-level linked images
        spread_files = [n for n in z.namelist() if re.match(r'Spreads/Spread_.*\.xml$', n)]
        for sf in sorted(spread_files):
            xml = z.read(sf).decode('utf-8')
            links = re.findall(r'LinkResourceURI="([^"]+)"', xml)
            for uri in links:
                fname = unquote(uri.split('/')[-1].split('\\')[-1])
                if fname and fname not in seen:
                    ordered.append(fname)
                    seen.add(fname)
                    all_files.add(fname)

    return ordered, all_files


def find_image_regions_on_page(page, min_w=MIN_IMG_W, min_h=MIN_IMG_H):
    """
    Return a list of (x0,y0,x1,y1) bounding boxes for image-like regions
    on the page — areas covered by vector drawings but NOT by text.
    """
    # Text bounding boxes (inflated a little)
    d = page.get_text("dict", flags=0)
    text_rects = []
    for b in d.get("blocks", []):
        if b.get("type") == 0:
            x0, y0, x1, y1 = b["bbox"]
            text_rects.append((x0 - 3, y0 - 3, x1 + 3, y1 + 3))

    # All drawing path bounding boxes
    drawings = page.get_drawings()
    draw_rects = []
    for dw in drawings:
        r = dw.get('rect')
        if r and r[2] > r[0] and r[3] > r[1]:
            draw_rects.append((r[0], r[1], r[2], r[3]))

    def overlaps_text(r, threshold=0.15):
        rx0, ry0, rx1, ry1 = r
        r_area = (rx1 - rx0) * (ry1 - ry0)
        if r_area <= 0:
            return True
        for t in text_rects:
            ox0 = max(rx0, t[0]); oy0 = max(ry0, t[1])
            ox1 = min(rx1, t[2]); oy1 = min(ry1, t[3])
            if ox1 > ox0 and oy1 > oy0:
                if (ox1 - ox0) * (oy1 - oy0) / r_area > threshold:
                    return True
        return False

    non_text = [r for r in draw_rects
                if not overlaps_text(r)
                and (r[2] - r[0]) >= min_w
                and (r[3] - r[1]) >= min_h]

    # Cluster nearby rects
    def cluster(rects, gap=CLUSTER_GAP):
        merged = list(rects)
        changed = True
        while changed:
            changed = False
            result = []
            used = [False] * len(merged)
            for i in range(len(merged)):
                if used[i]:
                    continue
                ax0, ay0, ax1, ay1 = merged[i]
                for j in range(i + 1, len(merged)):
                    if used[j]:
                        continue
                    bx0, by0, bx1, by1 = merged[j]
                    if (ax0 <= bx1 + gap and ax1 >= bx0 - gap and
                            ay0 <= by1 + gap and ay1 >= by0 - gap):
                        ax0 = min(ax0, bx0); ay0 = min(ay0, by0)
                        ax1 = max(ax1, bx1); ay1 = max(ay1, by1)
                        used[j] = True
                        changed = True
                result.append((ax0, ay0, ax1, ay1))
                used[i] = True
            merged = result
        return merged

    clusters = cluster(non_text)

    # Filter again after clustering
    final = [(x0, y0, x1, y1) for x0, y0, x1, y1 in clusters
             if (x1 - x0) >= min_w and (y1 - y0) >= min_h]

    # Sort top-to-bottom then left-to-right
    final.sort(key=lambda r: (r[1], r[0]))
    return final


def crop_region(page, rect, scale=RENDER_SCALE, padding=4):
    """Render a clipped region of the page and return a fitz.Pixmap."""
    x0, y0, x1, y1 = rect
    pad = padding
    clip = fitz.Rect(max(0, x0 - pad), max(0, y0 - pad),
                     min(page.rect.width, x1 + pad),
                     min(page.rect.height, y1 + pad))
    mat = fitz.Matrix(scale, scale)
    return page.get_pixmap(matrix=mat, clip=clip)


def deduplicate_regions(regions_by_page, repeat_skip=REPEAT_SKIP_PX):
    """
    Remove regions that appear on many pages with the same size (headers/footers like QR codes).
    `regions_by_page` is a list of (page_num, x0,y0,x1,y1).
    """
    from collections import Counter
    size_counts = Counter()
    for _, x0, y0, x1, y1 in regions_by_page:
        w = round(x1 - x0); h = round(y1 - y0)
        size_counts[(w, h)] += 1

    # Sizes appearing on 3+ pages are probably repeated decorations
    repeat_sizes = {s for s, c in size_counts.items() if c >= 3}

    filtered = []
    for entry in regions_by_page:
        _, x0, y0, x1, y1 = entry
        w = round(x1 - x0); h = round(y1 - y0)
        if (w, h) not in repeat_sizes:
            filtered.append(entry)
    return filtered


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    idml_path = sys.argv[1]
    pdf_path  = sys.argv[2]
    out_dir   = sys.argv[3] if len(sys.argv) > 3 else str(Path(idml_path).parent / 'extracted_images')

    os.makedirs(out_dir, exist_ok=True)

    print(f"Parsing IDML: {Path(idml_path).name}")
    ordered_images, all_image_files = parse_idml_images(idml_path)
    print(f"  Found {len(ordered_images)} linked images in document order")
    print(f"  First 5: {ordered_images[:5]}")

    print(f"\nScanning PDF: {Path(pdf_path).name}")
    doc = fitz.open(pdf_path)
    print(f"  Pages: {len(doc)}")

    # Collect all image regions from all pages
    all_regions = []  # (page_idx, x0,y0,x1,y1)
    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        regions = find_image_regions_on_page(page)
        for r in regions:
            all_regions.append((pg_idx, r[0], r[1], r[2], r[3]))
        if regions:
            print(f"  Page {pg_idx+1}: {len(regions)} image regions detected")

    # Remove repeated header/footer elements
    before = len(all_regions)
    all_regions = deduplicate_regions(all_regions)
    print(f"\nRegions: {before} total, {len(all_regions)} after dedup")

    # Match: zip ordered_images with all_regions in document order
    n_match = min(len(ordered_images), len(all_regions))
    print(f"\nMatching {n_match} images to {n_match} regions (order-based)")

    manifest = {}  # filename → output png path (relative)

    for i in range(n_match):
        fname = ordered_images[i]
        pg_idx, x0, y0, x1, y1 = all_regions[i]
        page = doc[pg_idx]

        pix = crop_region(page, (x0, y0, x1, y1))

        # Save as PNG named after original filename (stem only, + .png)
        stem = Path(fname).stem
        out_name = f"{stem}.png"
        out_path = os.path.join(out_dir, out_name)
        pix.save(out_path)

        # Also save under the original filename (for direct name match)
        orig_out = os.path.join(out_dir, fname + '.png') if not fname.endswith('.png') else os.path.join(out_dir, fname)
        if out_name != Path(orig_out).name:
            pix.save(orig_out)

        manifest[fname] = out_name
        if (i + 1) % 20 == 0 or i == n_match - 1:
            print(f"  [{i+1}/{n_match}] {fname} → page {pg_idx+1}")

    # Warn about unmatched
    if len(ordered_images) > len(all_regions):
        print(f"\nWARNING: {len(ordered_images)-len(all_regions)} images unmatched (not enough regions found)")
        for fname in ordered_images[len(all_regions):]:
            print(f"  UNMATCHED: {fname}")

    # Save manifest
    manifest_path = os.path.join(out_dir, '_manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({'matched': manifest, 'total_idml': len(ordered_images),
                   'total_regions': len(all_regions)}, f, indent=2)

    print(f"\nDone! {n_match} images saved to: {out_dir}")
    print(f"Upload the folder via the pipeline's 'Upload Image Folder' button (Step 2).")
    print(f"Manifest: {manifest_path}")


if __name__ == '__main__':
    main()
