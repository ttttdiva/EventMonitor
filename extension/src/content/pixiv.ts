import { PixivWorkData, ImportProgress, MessageResponse } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

// ===== 定数 =====
const BUTTON_ID = 'hydrus-import-btn';
const MESSAGE_CHUNK_SIZE = 4 * 1024 * 1024;
const ERROR_LABEL_MAX_LENGTH = 48;

// ===== ボタン状態管理 =====
type ButtonState = 'idle' | 'working' | 'done' | 'error';

function createFloatingButton(): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.id = BUTTON_ID;
  btn.textContent = 'H Import';
  btn.title = 'Import to Hydrus Network';

  // 画面右下に固定表示
  Object.assign(btn.style, {
    position: 'fixed',
    bottom: '20px',
    right: '20px',
    zIndex: '99999',
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '10px 20px',
    border: 'none',
    borderRadius: '24px',
    background: '#0096fa',
    color: '#fff',
    fontSize: '14px',
    fontWeight: '700',
    cursor: 'pointer',
    transition: 'all 0.2s',
    boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
    whiteSpace: 'nowrap',
  } as Record<string, string>);

  btn.addEventListener('mouseenter', () => {
    if (btn.dataset.state !== 'working') {
      btn.style.background = '#007acc';
    }
  });
  btn.addEventListener('mouseleave', () => {
    if (btn.dataset.state !== 'working') {
      btn.style.background = '#0096fa';
    }
  });

  return btn;
}

function updateButtonState(btn: HTMLButtonElement, state: ButtonState, detail?: string) {
  btn.dataset.state = state;
  btn.title = detail || 'Import to Hydrus Network';
  switch (state) {
    case 'idle':
      btn.textContent = 'H Import';
      btn.style.background = '#0096fa';
      btn.disabled = false;
      break;
    case 'working':
      btn.textContent = detail || 'Importing...';
      btn.style.background = '#f5a623';
      btn.disabled = true;
      break;
    case 'done':
      btn.textContent = detail || 'Done';
      btn.style.background = '#2ecc71';
      btn.disabled = false;
      break;
    case 'error':
      btn.textContent = detail || 'Error';
      btn.style.background = '#e74c3c';
      btn.disabled = false;
      break;
  }
}

function abbreviateButtonText(text: string): string {
  if (text.length <= ERROR_LABEL_MAX_LENGTH) {
    return text;
  }
  return `${text.slice(0, ERROR_LABEL_MAX_LENGTH - 3)}...`;
}

function updateButtonError(btn: HTMLButtonElement, message: string) {
  const detail = `Error: ${message}`;
  updateButtonState(btn, 'error', abbreviateButtonText(detail));
  btn.title = detail;
}

function responseErrorSummary(response: MessageResponse): string {
  const errors = response.data?.errors;
  if (Array.isArray(errors) && errors.length > 0) {
    return errors.join('; ');
  }
  return response.error || 'Import failed';
}

function prefixImageError(pageNumber: number, message: string): string {
  return /^Image \d+:/i.test(message) ? message : `Image ${pageNumber}: ${message}`;
}

function isMessageSizeError(response: MessageResponse): boolean {
  const message = `${response.error || ''} ${responseErrorSummary(response)}`.toLowerCase();
  return (
    message.includes('maximum allowed size') ||
    message.includes('64mib') ||
    (message.includes('message') && message.includes('size')) ||
    (message.includes('message') && message.includes('length'))
  );
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.byteLength; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

// ===== Pixiv AJAX API =====

function extractIllustId(): string | null {
  const match = window.location.pathname.match(/\/artworks\/(\d+)/);
  return match ? match[1] : null;
}

async function fetchPixivWorkData(illustId: string): Promise<PixivWorkData> {
  // メタデータ取得
  const metaResp = await fetch(`https://www.pixiv.net/ajax/illust/${illustId}`, {
    credentials: 'include',
  });
  if (!metaResp.ok) {
    throw new Error(`Pixiv API error: ${metaResp.status}`);
  }
  const metaJson = await metaResp.json();
  if (metaJson.error) {
    throw new Error(`Pixiv API: ${metaJson.message || 'Unknown error'}`);
  }
  const body = metaJson.body;

  // ページURL取得 (複数ページ対応)
  const pagesResp = await fetch(`https://www.pixiv.net/ajax/illust/${illustId}/pages`, {
    credentials: 'include',
  });
  if (!pagesResp.ok) {
    throw new Error(`Pixiv pages API error: ${pagesResp.status}`);
  }
  const pagesJson = await pagesResp.json();
  const imageUrls: string[] = pagesJson.body.map(
    (p: { urls: { original: string } }) => p.urls.original
  );

  // タグ抽出
  const tags: string[] = body.tags.tags.map(
    (t: { tag: string }) => t.tag
  );

  return {
    id: String(body.id || illustId),
    title: body.title || '',
    userName: body.userName || '',
    userId: String(body.userId || ''),
    tags,
    xRestrict: body.xRestrict || 0,
    pageCount: body.pageCount || imageUrls.length,
    imageUrls,
    sourceUrl: `https://www.pixiv.net/artworks/${illustId}`,
  };
}

// ===== ボタン管理 =====

let currentIllustId: string | null = null;

function showButton() {
  const illustId = extractIllustId();
  if (!illustId) {
    // artworksページでなければボタンを非表示
    const existing = document.getElementById(BUTTON_ID);
    if (existing) existing.style.display = 'none';
    return;
  }

  let btn = document.getElementById(BUTTON_ID) as HTMLButtonElement | null;

  if (!btn) {
    // 初回: ボタン生成してbodyに追加
    btn = createFloatingButton();
    document.body.appendChild(btn);
    btn.addEventListener('click', () => {
      const id = extractIllustId();
      if (id) handleImport(btn!, id);
    });
  }

  // ページ遷移で別の作品に移った場合はリセット
  if (currentIllustId !== illustId) {
    currentIllustId = illustId;
    updateButtonState(btn, 'idle');
  }

  btn.style.display = 'flex';
}

// ===== インポート処理 =====

async function downloadImagesAsBase64(
  urls: string[],
  btn: HTMLButtonElement
): Promise<string[]> {
  const results: string[] = [];
  for (let i = 0; i < urls.length; i++) {
    updateButtonState(btn, 'working', `DL ${i + 1}/${urls.length}`);
    const resp = await fetch(urls[i], { referrer: 'https://www.pixiv.net/' });
    if (!resp.ok) {
      throw new Error(`Image DL failed: ${resp.status} (${i + 1}/${urls.length})`);
    }
    results.push(arrayBufferToBase64(await resp.arrayBuffer()));
  }
  return results;
}

function createTransferId(workId: string, pageNumber: number): string {
  const randomPart =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `pixiv-${workId}-${pageNumber}-${randomPart}`;
}

function assertBackgroundResponse(response: MessageResponse, context: string): void {
  if (!response.success) {
    throw new Error(`${context}: ${response.error || 'background request failed'}`);
  }
}

async function importPixivImageChunked(
  workData: PixivWorkData,
  imageUrl: string,
  imageData: string,
  pageNumber: number
): Promise<MessageResponse> {
  const transferId = createTransferId(workData.id, pageNumber);
  const totalChunks = Math.ceil(imageData.length / MESSAGE_CHUNK_SIZE);
  const pageWork: PixivWorkData = {
    ...workData,
    imageUrls: [imageUrl],
    pageCount: 1,
  };

  try {
    assertBackgroundResponse(
      await sendToBackground({
        type: 'IMPORT_PIXIV_IMAGE_CHUNKED_START',
        transferId,
        data: pageWork,
        totalChunks,
      }),
      'Pixiv chunked import start failed'
    );

    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
      const start = chunkIndex * MESSAGE_CHUNK_SIZE;
      const chunk = imageData.slice(start, start + MESSAGE_CHUNK_SIZE);
      assertBackgroundResponse(
        await sendToBackground({
          type: 'IMPORT_PIXIV_IMAGE_CHUNK',
          transferId,
          chunkIndex,
          chunk,
        }),
        `Pixiv chunk transfer failed (${chunkIndex + 1}/${totalChunks})`
      );
    }

    return await sendToBackground({
      type: 'IMPORT_PIXIV_IMAGE_CHUNKED_FINISH',
      transferId,
    });
  } catch (e) {
    await sendToBackground({ type: 'IMPORT_PIXIV_IMAGE_CHUNKED_ABORT', transferId });
    throw e;
  }
}

async function importPixivWorkFastPath(
  workData: PixivWorkData,
  imageDataList: string[]
): Promise<MessageResponse> {
  return await sendToBackground({
    type: 'IMPORT_PIXIV_WORK',
    data: workData,
    imageDataList,
  });
}

async function importPixivWorkChunkFallback(
  btn: HTMLButtonElement,
  workData: PixivWorkData,
  imageDataList: string[]
): Promise<{ imported: number; errors: string[] }> {
  let imported = 0;
  const errors: string[] = [];

  for (let i = 0; i < workData.imageUrls.length; i++) {
    const pageNumber = i + 1;
    updateButtonState(btn, 'working', `Import ${pageNumber}/${workData.imageUrls.length}`);
    const response = await importPixivImageChunked(
      workData,
      workData.imageUrls[i],
      imageDataList[i],
      pageNumber
    );
    if (response.success) {
      imported += Number(response.data?.imported || 0);
      if (Array.isArray(response.data?.errors)) {
        errors.push(...response.data.errors);
      }
    } else {
      errors.push(prefixImageError(pageNumber, responseErrorSummary(response)));
    }
  }

  return { imported, errors };
}

async function handleImport(btn: HTMLButtonElement, illustId: string) {
  if (btn.dataset.state === 'working') return;

  updateButtonState(btn, 'working', 'Fetching...');

  try {
    const workData = await fetchPixivWorkData(illustId);
    const total = workData.imageUrls.length;

    const imageDataList = await downloadImagesAsBase64(workData.imageUrls, btn);
    updateButtonState(btn, 'working', `Import 0/${total}`);

    let response = await importPixivWorkFastPath(workData, imageDataList);
    let imported = Number(response.data?.imported || 0);
    let errors = Array.isArray(response.data?.errors) ? [...response.data.errors] : [];

    if (!response.success && imported === 0) {
      if (!isMessageSizeError(response)) {
        errors.push(responseErrorSummary(response));
      } else {
        ({ imported, errors } = await importPixivWorkChunkFallback(btn, workData, imageDataList));
      }
    }

    if (imported > 0) {
      updateButtonState(btn, 'done', `Done (${imported}/${total})`);
      btn.title = errors.length > 0
        ? `Imported with errors: ${errors.join('; ')}`
        : `Imported ${imported}/${total}`;
      if (errors.length > 0) {
        console.warn('[Hydrus Importer] Pixiv import completed with errors:', errors);
      }
    } else {
      const errorMessage = errors[0] || 'No files imported';
      updateButtonError(btn, errorMessage);
      console.error('[Hydrus Importer] Pixiv import error:', errorMessage);
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    updateButtonError(btn, msg);
    console.error('[Hydrus Importer] Pixiv import error:', msg);
  }
}

// ===== 進捗受信 =====

chrome.runtime.onMessage.addListener((message: { type: string; progress?: ImportProgress }) => {
  if (message.type === 'IMPORT_PROGRESS' && message.progress) {
    const progress = message.progress;
    const btn = document.getElementById(BUTTON_ID) as HTMLButtonElement | null;
    if (!btn) return;

    switch (progress.phase) {
      case 'downloading':
        updateButtonState(btn, 'working', `DL ${progress.current}/${progress.total}`);
        break;
      case 'importing':
        updateButtonState(btn, 'working', `Import ${progress.current}/${progress.total}`);
        break;
      case 'tagging':
        updateButtonState(btn, 'working', `Tag ${progress.current}/${progress.total}`);
        break;
      case 'error':
        if (progress.errorMessage) {
          updateButtonError(btn, progress.errorMessage);
        }
        break;
    }
  }
});

// ===== SPA ナビゲーション対応 =====

let lastUrl = location.href;

const observer = new MutationObserver(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href;
    setTimeout(showButton, 300);
  }
});

observer.observe(document.body, { childList: true, subtree: true });

// 初期表示
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(showButton, 300));
} else {
  setTimeout(showButton, 300);
}
