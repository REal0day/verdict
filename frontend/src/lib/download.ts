/**
 * Authenticated file download.
 *
 * Browser `<a href>` navigation can't carry an Authorization header, so the
 * existing /ui/reports/{id}/download and /attachments/{id}/download endpoints
 * (which fall back to cookie auth) only work when the user has the Jinja
 * cookie. For pure-SPA sessions, fetch the bytes ourselves with the Bearer
 * token, then trigger a download via a blob URL.
 */
import { getToken } from "./api";

export async function downloadFile(url: string, suggestedFilename?: string): Promise<void> {
  const tok = getToken();
  const resp = await fetch(url, {
    headers: tok ? { Authorization: `Bearer ${tok}` } : {},
  });
  if (!resp.ok) {
    throw new Error(`Download failed: ${resp.status} ${resp.statusText}`);
  }

  // Prefer the filename the server suggested via Content-Disposition.
  let filename = suggestedFilename || "download";
  const cd = resp.headers.get("content-disposition") || "";
  const m = /filename="?([^"]+)"?/i.exec(cd);
  if (m) filename = m[1];

  const blob = await resp.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Defer revoke so Firefox/Safari finish the download first.
  setTimeout(() => URL.revokeObjectURL(blobUrl), 2000);
}
