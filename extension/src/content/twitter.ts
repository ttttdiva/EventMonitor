import { TwitterTweetData, ImportProgress } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

// ===== 定数 =====
const BUTTON_ATTR = 'data-hydrus-import';
const PROCESSED_ATTR = 'data-hydrus-processed';

// ===== ボタン状態管理 =====
type ButtonState = 'idle' | 'working' | 'done' | 'error';

function createTweetButton(): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.setAttribute(BUTTON_ATTR, 'true');
  btn.textContent = 'H';
  btn.title = 'Import to Hydrus';

  // Twitterのアクションボタンに合わせたスタイル
  Object.assign(btn.style, {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '34px',
    height: '34px',
    border: '1px solid #536471',
    borderRadius: '50%',
    background: 'transparent',
    color: '#536471',
    fontSize: '13px',
    fontWeight: '700',
    cursor: 'pointer',
    transition: 'all 0.2s',
    marginLeft: '4px',
    padding: '0',
    lineHeight: '1',
  } as Record<string, string>);

  btn.addEventListener('mouseenter', () => {
    if (btn.dataset.state !== 'working') {
      btn.style.background = 'rgba(0, 150, 250, 0.1)';
      btn.style.color = '#0096fa';
      btn.style.borderColor = '#0096fa';
    }
  });
  btn.addEventListener('mouseleave', () => {
    if (btn.dataset.state !== 'working') {
      btn.style.background = 'transparent';
      btn.style.color = '#536471';
      btn.style.borderColor = '#536471';
    }
  });

  return btn;
}

function updateTweetButtonState(btn: HTMLButtonElement, state: ButtonState, detail?: string) {
  btn.dataset.state = state;
  switch (state) {
    case 'idle':
      btn.textContent = 'H';
      btn.style.borderColor = '#536471';
      btn.style.color = '#536471';
      btn.style.background = 'transparent';
      btn.disabled = false;
      break;
    case 'working':
      btn.textContent = '...';
      btn.style.borderColor = '#f5a623';
      btn.style.color = '#fff';
      btn.style.background = '#f5a623';
      btn.disabled = true;
      btn.title = detail || 'Importing...';
      break;
    case 'done':
      btn.textContent = '\u2713';
      btn.style.borderColor = '#2ecc71';
      btn.style.color = '#fff';
      btn.style.background = '#2ecc71';
      btn.disabled = false;
      btn.title = detail || 'Done';
      break;
    case 'error':
      btn.textContent = '!';
      btn.style.borderColor = '#e74c3c';
      btn.style.color = '#fff';
      btn.style.background = '#e74c3c';
      btn.disabled = false;
      btn.title = detail || 'Error - click to retry';
      break;
  }
}

// ===== DOM からメタデータ抽出 =====

function extractTweetIdFromUrl(url?: string): string | null {
  const target = url || window.location.href;
  const match = target.match(/\/status\/(\d+)/);
  return match ? match[1] : null;
}

function extractTweetData(article: Element): TwitterTweetData | null {
  // ツイートリンクからIDを取得
  const tweetLink = article.querySelector('a[href*="/status/"]') as HTMLAnchorElement | null;
  if (!tweetLink) return null;

  const tweetId = extractTweetIdFromUrl(tweetLink.href);
  if (!tweetId) return null;

  // ユーザー情報
  let username = '';
  let displayName = '';

  // ステータスリンクからusernameを抽出（最も信頼性が高い）
  const statusMatch = tweetLink.href.match(/(?:x\.com|twitter\.com)\/([^/]+)\/status\//);
  if (statusMatch) {
    username = statusMatch[1];
  }

  // display_name: User-Nameコンテナから取得
  const nameContainer = article.querySelector('[data-testid="User-Name"]');
  if (nameContainer) {
    // 最初のリンクからdisplay name取得
    const nameLinks = nameContainer.querySelectorAll('a[role="link"]');
    if (nameLinks.length > 0) {
      const firstLink = nameLinks[0];
      // span内のテキストを取得（絵文字imgは除外）
      const spans = firstLink.querySelectorAll('span');
      const nameParts: string[] = [];
      spans.forEach(span => {
        if (span.textContent && !span.querySelector('span')) {
          nameParts.push(span.textContent);
        }
      });
      displayName = nameParts.join('') || firstLink.textContent || '';
    }
    // usernameのフォールバック: @付きのリンクから取得
    if (!username && nameLinks.length > 1) {
      const handleText = nameLinks[1].textContent || '';
      username = handleText.replace(/^@/, '');
    }
  }

  // usernameのフォールバック: 任意のユーザーリンクから
  if (!username) {
    const userLinkEl = article.querySelector(
      'a[role="link"][href^="/"]'
    ) as HTMLAnchorElement | null;
    if (userLinkEl) {
      const href = userLinkEl.getAttribute('href') || '';
      username = href.replace(/^\//, '').split('/')[0];
    }
  }

  // ツイート本文
  const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
  const text = tweetTextEl?.textContent || '';

  // 画像URL取得
  const imageUrls = extractImageUrls(article);
  if (imageUrls.length === 0) return null; // 画像なしツイートはスキップ

  // センシティブ判定
  const sensitive = !!article.querySelector('[data-testid="sensitiveMediaWarning"]');

  const sourceUrl = `https://x.com/${username}/status/${tweetId}`;

  return {
    tweetId,
    username,
    displayName: displayName || username,
    text,
    imageUrls,
    sensitive,
    sensitiveFlags: sensitive ? ['sensitive'] : [],
    sourceUrl,
  };
}

function extractImageUrls(article: Element): string[] {
  const urls: string[] = [];

  // 方法1: data-testid="tweetPhoto" 内の img
  const tweetPhotos = article.querySelectorAll(
    '[data-testid="tweetPhoto"] img'
  );

  tweetPhotos.forEach(img => {
    const src = img.getAttribute('src') || '';
    // srcset からも取得（Twitter/Xが srcset を使う場合がある）
    const srcset = img.getAttribute('srcset') || '';

    if (src.includes('pbs.twimg.com/media/')) {
      urls.push(upgradeToOriginal(src));
    } else if (srcset.includes('pbs.twimg.com/media/')) {
      // srcsetから最大解像度のURLを抽出
      const srcsetUrl = extractBestFromSrcset(srcset);
      if (srcsetUrl) urls.push(upgradeToOriginal(srcsetUrl));
    }
  });

  // 方法2: tweetPhoto内の picture > source (WebP対応)
  if (urls.length === 0) {
    const sources = article.querySelectorAll('[data-testid="tweetPhoto"] picture source');
    sources.forEach(source => {
      const srcset = source.getAttribute('srcset') || '';
      if (srcset.includes('pbs.twimg.com/media/')) {
        const srcsetUrl = extractBestFromSrcset(srcset);
        if (srcsetUrl) urls.push(upgradeToOriginal(srcsetUrl));
      }
    });
  }

  // 方法3: フォールバック - article内の全画像からtwimg media URLを収集
  if (urls.length === 0) {
    const allImgs = article.querySelectorAll('img');
    allImgs.forEach(img => {
      const src = img.getAttribute('src') || '';
      if (src.includes('pbs.twimg.com/media/')) {
        // プロフィール画像やアイコンを除外（小さすぎるもの）
        const width = img.naturalWidth || parseInt(img.getAttribute('width') || '0', 10);
        if (width === 0 || width > 100) {
          urls.push(upgradeToOriginal(src));
        }
      }
    });
  }

  return [...new Set(urls)]; // 重複除去
}

/**
 * srcset属性から最適なURLを抽出
 * 例: "url1 1x, url2 2x" → url2
 */
function extractBestFromSrcset(srcset: string): string | null {
  const entries = srcset.split(',').map(s => s.trim());
  let bestUrl = '';
  let bestRes = 0;

  for (const entry of entries) {
    const parts = entry.split(/\s+/);
    if (parts.length >= 1 && parts[0].includes('pbs.twimg.com/media/')) {
      const resStr = parts[1] || '1x';
      const res = parseFloat(resStr) || 1;
      if (res >= bestRes) {
        bestRes = res;
        bestUrl = parts[0];
      }
    }
  }

  return bestUrl || null;
}

/**
 * 画像URLをオリジナルサイズに変換
 * small/medium/large → orig
 */
function upgradeToOriginal(url: string): string {
  try {
    const u = new URL(url);
    u.searchParams.set('name', 'orig');
    // format が無い場合はjpgをデフォルトに
    if (!u.searchParams.has('format')) {
      u.searchParams.set('format', 'jpg');
    }
    return u.toString();
  } catch {
    // URLパース失敗時はname=origを付加
    if (url.includes('?')) {
      return url.replace(/name=\w+/, 'name=orig');
    }
    return url + '?name=orig';
  }
}

// ===== コンテンツスクリプト側で画像ダウンロード (認証Cookie付き) =====

async function downloadImageAsBase64(url: string): Promise<string> {
  const resp = await fetch(url, { credentials: 'include' });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} for ${url}`);
  }
  const blob = await resp.blob();
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const dataUrl = reader.result as string;
      // "data:image/jpeg;base64,..." → Base64部分のみ
      const base64 = dataUrl.split(',')[1];
      if (base64) {
        resolve(base64);
      } else {
        reject(new Error('Failed to convert to Base64'));
      }
    };
    reader.onerror = () => reject(new Error('FileReader error'));
    reader.readAsDataURL(blob);
  });
}

// ===== ボタン注入 =====

function hasMediaImages(article: Element): boolean {
  // data-testid="tweetPhoto" 内に画像があるか
  const tweetPhotoImgs = article.querySelectorAll('[data-testid="tweetPhoto"] img');
  for (const img of Array.from(tweetPhotoImgs)) {
    const src = img.getAttribute('src') || '';
    const srcset = img.getAttribute('srcset') || '';
    if (src.includes('pbs.twimg.com/media/') || srcset.includes('pbs.twimg.com/media/')) {
      return true;
    }
  }

  // picture > source のフォールバック
  const sources = article.querySelectorAll('[data-testid="tweetPhoto"] picture source');
  for (const source of Array.from(sources)) {
    const srcset = source.getAttribute('srcset') || '';
    if (srcset.includes('pbs.twimg.com/media/')) {
      return true;
    }
  }

  // 全img要素のフォールバック
  const allImgs = article.querySelectorAll('img');
  for (const img of Array.from(allImgs)) {
    const src = img.getAttribute('src') || '';
    if (src.includes('pbs.twimg.com/media/')) {
      const width = img.naturalWidth || parseInt(img.getAttribute('width') || '0', 10);
      if (width === 0 || width > 100) {
        return true;
      }
    }
  }

  return false;
}

function findTweetArticles(): Element[] {
  return Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
}

function injectButtons() {
  const articles = findTweetArticles();

  for (const article of articles) {
    // 既に処理済みならスキップ
    if (article.getAttribute(PROCESSED_ATTR)) continue;

    // 画像があるツイートのみ
    if (!hasMediaImages(article)) continue;

    // アクションバー（いいね等のボタンがある行）を探す
    // role="group" が標準だが、フォールバックも用意
    let actionBar = article.querySelector('[role="group"]');
    if (!actionBar) {
      // フォールバック: ツイートアクションのコンテナ
      actionBar = article.querySelector('[data-testid="reply"], [data-testid="retweet"], [data-testid="like"]')?.parentElement || null;
    }
    if (!actionBar) continue;

    article.setAttribute(PROCESSED_ATTR, 'true');

    const btn = createTweetButton();
    actionBar.appendChild(btn);

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      handleTweetImport(btn, article);
    });
  }
}

// ===== インポート処理 =====

async function handleTweetImport(btn: HTMLButtonElement, article: Element) {
  if (btn.dataset.state === 'working') return;

  // done状態なら再インポートせずスキップ
  if (btn.dataset.state === 'done') return;

  updateTweetButtonState(btn, 'working');

  try {
    const tweetData = extractTweetData(article);
    if (!tweetData) {
      updateTweetButtonState(btn, 'error', 'Could not extract tweet data');
      return;
    }

    // コンテンツスクリプト側で画像をダウンロード（認証Cookie付き）
    // Twitter/XのCDNはService Worker(background)からの直接fetchが失敗するため
    const imageDataList: string[] = [];
    let downloadFailed = false;

    for (let i = 0; i < tweetData.imageUrls.length; i++) {
      try {
        updateTweetButtonState(btn, 'working', `Downloading ${i + 1}/${tweetData.imageUrls.length}...`);
        const base64 = await downloadImageAsBase64(tweetData.imageUrls[i]);
        imageDataList.push(base64);
      } catch (e) {
        console.warn('[Hydrus Importer] Content-side download failed, falling back to background:', e);
        downloadFailed = true;
        break;
      }
    }

    updateTweetButtonState(btn, 'working', 'Importing to Hydrus...');

    const response = await sendToBackground({
      type: 'IMPORT_TWITTER_TWEET',
      data: tweetData,
      // DL成功分のみ送信、失敗時はbackground側でリトライ
      imageDataList: downloadFailed ? undefined : imageDataList,
    });

    if (response.success) {
      const { imported, total } = response.data;
      updateTweetButtonState(btn, 'done', `Imported ${imported}/${total}`);
    } else {
      updateTweetButtonState(btn, 'error', response.error || 'Import failed');
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    updateTweetButtonState(btn, 'error', msg);
    console.error('[Hydrus Importer] Twitter import error:', msg);
  }
}

// ===== 進捗受信 =====

chrome.runtime.onMessage.addListener((message: { type: string }) => {
  if (message.type === 'IMPORT_PROGRESS') {
    // Twitter はタイムラインに複数ボタンがあるため、
    // 進捗はsendToBackgroundの最終レスポンスで反映する
    // (個別画像の進捗表示はボタンサイズの制約上省略)
  }
});

// ===== MutationObserver でタイムライン動的読み込み対応 =====

// debounce付きで過剰なDOM変更イベントに対応
let injectTimer: ReturnType<typeof setTimeout> | null = null;

const observer = new MutationObserver(() => {
  if (injectTimer) clearTimeout(injectTimer);
  injectTimer = setTimeout(injectButtons, 200);
});

observer.observe(document.body, { childList: true, subtree: true });

// 初期注入
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(injectButtons, 500));
} else {
  setTimeout(injectButtons, 500);
}
