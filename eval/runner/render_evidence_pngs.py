"""把报告引证的 PDF 页渲染成带高亮矩形的 PNG(离线演示用,pypdfium2+PIL,无浏览器依赖)。

用法: python3 render_evidence_pngs.py --arm-dir e2e_b1 --top 5
产物: eval/results/demo/pages/<CVxx>_p<N>_<span>.png + pages_manifest.json
"""
import argparse
import json
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

RES = Path(__file__).resolve().parents[1] / "results"
PDFS = Path.home() / "Desktop/简历库/resumes"
SCALE = 2.0


def render_page_png(pdf_path: Path, page_no: int, rects: list[dict], out: Path):
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf[page_no - 1]
    bitmap = page.render(scale=SCALE)
    img = bitmap.to_pil().convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for r in rects:
        x0, y0 = r["x0"] * SCALE, r["top"] * SCALE
        x1, y1 = r["x1"] * SCALE, r["bottom"] * SCALE
        d.rectangle([x0, y0, x1, y1], fill=(255, 220, 0, 80), outline=(200, 40, 40, 240), width=3)
    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(out, "PNG")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm-dir", default="e2e_b1")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()
    arm = RES / args.arm_dir
    outdir = RES / "demo" / "pages"
    outdir.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(l) for l in (arm / "summary.jsonl").read_text(encoding="utf-8").splitlines()]
    rows.sort(key=lambda r: -r["score"])
    manifest = {}
    n = 0
    for r in rows[: args.top]:
        rep = json.loads((arm / f"report_{r['candidate']}.json").read_text(encoding="utf-8"))
        fname = rep["candidate"]["filename"]
        pdf_path = next(PDFS.glob(fname), None)
        if not pdf_path:
            continue
        seen = set()
        for it in rep["items"]:
            for c in it["citations"][:1]:
                key = f"{r['candidate']}_p{c['page']}_{c['span_id']}"
                if key in seen:
                    continue
                seen.add(key)
                png = outdir / f"{key}.png"
                render_page_png(pdf_path, c["page"], c["rects"], png)
                n += 1
                manifest.setdefault(r["candidate"], {})[f"{it['criterion_id']}|{c['span_id']}"] = f"pages/{png.name}"
    (RES / "demo" / "pages_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("rendered:", n, "pages →", outdir)


if __name__ == "__main__":
    main()
