// pdf.js, bundled locally (not from a CDN). Vite resolves the worker to a local
// asset via the ?url import, so the studio has no external runtime dependency.
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

export { pdfjsLib };

/**
 * Render every page of a PDF (given as an ArrayBuffer) to canvases.
 * Returns an array of { pageNumber, canvas, widthPt, heightPt, scale }.
 * widthPt/heightPt are the page size in PDF points (the same units the
 * manifest bounding boxes use), so overlays can be positioned exactly.
 */
export async function renderAllPages(arrayBuffer, scale = 1.5) {
  const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
  const pages = [];
  for (let n = 1; n <= pdf.numPages; n++) {
    const page = await pdf.getPage(n);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const ctx = canvas.getContext("2d");
    await page.render({ canvasContext: ctx, viewport }).promise;
    const unscaled = page.getViewport({ scale: 1 });
    pages.push({
      pageNumber: n,
      canvas,
      widthPt: unscaled.width,
      heightPt: unscaled.height,
      scale,
    });
  }
  return pages;
}
