"""
convert_links_to_png.py
-----------------------
Converts all EPS and WMF image files in a Links folder to PNG,
ready for upload to the Content Pipeline via the folder-upload button.

Usage:
    python convert_links_to_png.py <links_folder> [output_folder]

    links_folder  : Path to the InDesign Links/ folder containing .eps and .wmf files
    output_folder : Where to save PNGs (default: links_folder/../extracted_images)

EPS strategy : Extract the embedded TIFF preview stored in the binary EPS header.
WMF strategy : Render via Windows .NET System.Drawing (calls PowerShell internally).
PNG files    : Named as original_filename.png (e.g. Chem.1_1.wmf.png).
               The pipeline's findImage() ignores extensions, so it will match
               'Chem.1_1.wmf' → 'Chem.1_1.wmf.png' automatically.
"""

import os
import sys
import struct
import subprocess
import json
import tempfile
from pathlib import Path

try:
    import fitz   # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run:  pip install pymupdf")
    sys.exit(1)


# ─── EPS extraction ──────────────────────────────────────────────────────────

def extract_eps_to_png(eps_path: str, out_path: str, min_size=20) -> bool:
    """
    Extract embedded TIFF preview from binary EPS and save as PNG.
    Returns True on success.
    """
    try:
        with open(eps_path, 'rb') as f:
            header = f.read(28)

        # Binary EPS magic: C5 D0 D3 C6
        if header[:4] != b'\xc5\xd0\xd3\xc6':
            return False

        tiff_start, tiff_len = struct.unpack_from('<II', header, 20)
        if tiff_len < 100:
            return False

        with open(eps_path, 'rb') as f:
            f.seek(tiff_start)
            tiff_bytes = f.read(tiff_len)

        # Validate TIFF magic (II = little-endian, MM = big-endian)
        if tiff_bytes[:2] not in (b'II', b'MM'):
            return False

        pix = fitz.Pixmap(tiff_bytes)
        if pix.width < min_size or pix.height < min_size:
            return False

        # Convert to RGB if necessary (some TIFFs are CMYK)
        if pix.n > 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        elif pix.colorspace and pix.colorspace.name != 'DeviceRGB':
            pix = fitz.Pixmap(fitz.csRGB, pix)

        pix.save(out_path)
        return True

    except Exception:
        return False


# ─── WMF batch conversion via PowerShell ─────────────────────────────────────

WMF_BATCH_SCRIPT = r"""
param([string]$jsonPath)
Add-Type -AssemblyName System.Drawing
$pairs = Get-Content $jsonPath | ConvertFrom-Json
$results = @{}
foreach ($pair in $pairs) {
    $src = $pair.src
    $dst = $pair.dst
    try {
        $img = [System.Drawing.Image]::FromFile($src)
        $w   = [int]$img.Width
        $h   = [int]$img.Height
        if ($w -lt 4 -or $h -lt 4) { $img.Dispose(); $results[$src] = 'too_small'; continue }
        # Scale up if very low-res WMF (GDI default is often 96dpi; we want ≥150dpi equivalent)
        $scale = 1
        if ($w -lt 200 -or $h -lt 80) { $scale = 3 }
        elseif ($w -lt 400 -or $h -lt 150) { $scale = 2 }
        $bw = $w * $scale; $bh = $h * $scale
        $bmp = New-Object System.Drawing.Bitmap($bw, $bh)
        $bmp.SetResolution(150, 150)
        $g   = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($img, 0, 0, $bw, $bh)
        $bmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose(); $img.Dispose()
        $results[$src] = 'ok'
    } catch {
        $results[$src] = "error: $_"
    }
}
$results | ConvertTo-Json | Write-Output
"""


def convert_wmf_batch(pairs: list) -> dict:
    """
    Convert a list of {src, dst} WMF→PNG pairs using PowerShell.
    Returns dict of src→'ok'/'error:...' results.
    """
    if not pairs:
        return {}

    # Write pairs JSON
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as jf:
        json.dump(pairs, jf)
        json_path = jf.name

    # Write PS1 script
    with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as ps:
        ps.write(WMF_BATCH_SCRIPT)
        ps1_path = ps.name

    try:
        result = subprocess.run(
            ['powershell', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', ps1_path, '-jsonPath', json_path],
            capture_output=True, text=True, timeout=300
        )
        if result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
    except Exception as e:
        print(f"  PowerShell batch error: {e}")
    finally:
        os.unlink(json_path)
        os.unlink(ps1_path)

    return {}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    links_dir = Path(sys.argv[1])
    out_dir   = Path(sys.argv[2]) if len(sys.argv) > 2 else links_dir.parent / 'extracted_images'
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Source : {links_dir}")
    print(f"Output : {out_dir}\n")

    eps_files = sorted(links_dir.glob('*.eps')) + sorted(links_dir.glob('*.EPS'))
    wmf_files = sorted(links_dir.glob('*.wmf')) + sorted(links_dir.glob('*.WMF'))
    print(f"Found  : {len(eps_files)} EPS, {len(wmf_files)} WMF files")

    # ── EPS → PNG ──
    eps_ok = eps_fail = 0
    eps_need_sysdraw = []
    print(f"\n[1/2] Converting EPS files via embedded TIFF preview…")
    for ep in eps_files:
        out_path = out_dir / (ep.stem + '.png')  # stem only, no double extension
        if out_path.exists():
            eps_ok += 1; continue
        if extract_eps_to_png(str(ep), str(out_path)):
            eps_ok += 1
        else:
            # Fallback: try System.Drawing (handles EPS files with WMF-format headers)
            eps_need_sysdraw.append(ep)
            eps_fail += 1
    print(f"  EPS : {eps_ok} via TIFF, {eps_fail} need fallback")

    if eps_need_sysdraw:
        print(f"  Trying System.Drawing fallback for {len(eps_need_sysdraw)} EPS…")
        sd_pairs = [{'src': str(ep), 'dst': str(out_dir / (ep.stem + '.png'))}
                    for ep in eps_need_sysdraw]
        sd_results = convert_wmf_batch(sd_pairs)
        recovered = sum(1 for v in sd_results.values() if v == 'ok')
        print(f"  EPS fallback: {recovered} additional converted")

    # ── WMF → PNG ──
    print(f"\n[2/2] Converting WMF files via Windows System.Drawing…")
    wmf_pairs = []
    for wf in wmf_files:
        out_path = out_dir / (wf.stem + '.png')  # stem only
        wmf_pairs.append({'src': str(wf), 'dst': str(out_path)})

    # Process in batches of 50 to avoid PowerShell timeout
    BATCH = 50
    wmf_ok = wmf_fail = 0
    for i in range(0, len(wmf_pairs), BATCH):
        batch = wmf_pairs[i:i + BATCH]
        print(f"  Batch {i//BATCH + 1}/{(len(wmf_pairs)+BATCH-1)//BATCH} ({len(batch)} files)…", end=' ', flush=True)
        results = convert_wmf_batch(batch)
        ok = sum(1 for v in results.values() if v == 'ok')
        fail = len(batch) - ok
        wmf_ok += ok; wmf_fail += fail
        print(f"{ok} ok, {fail} failed")

    # ── PNG count ──
    png_files = list(out_dir.glob('*.png'))
    print(f"\n{'='*50}")
    print(f"Done! {len(png_files)} PNG files saved to:\n  {out_dir}")
    print(f"\nNext step:")
    print(f"  In the pipeline, go to Step 2 and click 'Upload Image Folder',")
    print(f"  then select the folder:  {out_dir}")


if __name__ == '__main__':
    main()
