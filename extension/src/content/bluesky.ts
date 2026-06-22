import { BlueskyPostData, ImportProgress } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

// ===== 定数 =====
const BUTTON_ATTR = 'data-hydrus-import';
const PROCESSED_ATTR = 'data-hydrus-processed';

// Blueskyのセンシティブラベル値
const SENSITIVE_LABELS = new Set([
  'sexual', 'nudity', 'graphic-violence',
  'graphic-media', 'gore', 'self-harm', 'porn',
]);

// ===== ボタン状態管理 =====
type ButtonState = 'idle' | 'working' | 'done' | 'error';

function createPostButton(): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.setAttribute(BUTTON_ATTR, 'true');
  btn.textContent = 'H';
  btn.title = 'Import to Hydrus';

  Object.assign(btn.style, {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '34px',
    height: '34px',
    border: '1px solid #687684',
    borderRadius: '50%',
    background: 'transparent',
    color: '#687684',
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
      btn.style.background = 'rgba(0, 133, 255, 0.1)';
      btn.style.color = '#0085ff';
      btn.style.borderColor = '#0085ff';
    }
  });
  btn.addEventListener('mouseleave', () => {
    if (btn.dataset.state !== 'working') {
      btn.style.background = 'transparent';
      btn.style.color = '#687684';
      btn.style.borderColor = '#687684';
    }
  });

  return btn;
}

function updatePostButtonState(btn: HTMLButtonElement, state: ButtonState, detail?: string) {
  btn.dataset.state = state;
  switch (state) {
    case 'idle':
      btn.textContent = 'H';
      btn.style.borderColor = '#687684';
      btn.style.color = '#687684';
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

// ===== AT Protocol Public API =====

interface BskyAuthor {
  did: string;
  handle: string;
  displayName?: string;
  labels?: Array<{ val: string }>;
}

interface BskyEmbed {
  $type: string;
  images?: Array<{
    alt: string;
    fullsize: string;
    thumb: string;
  }>;
  media?: {
    $type: string;
    images?: Array<{
      alt: string;
      fullsize: string;
      thumb: string;
    }>;
  };
}

interface BskyPost {
  uri: string;
  cid: string;
  author: BskyAuthor;
  record: {
    text: string;
    createdAt: string;
    facets?: Array<{
      features: Array<{ $type: string; tag?: string }>;
    }>;
    labels?: {
      values?: Array<{ val: string }>;
    };
  };
  embed?: BskyEmbed;
  labels?: Array<{ val: string }>;
}

function extractPostInfo(): { handle: string; postId: string } | null {
  const match = window.location.pathname.match(/\/profile\/([^/]+)\/post\/([^/]+)/);
  if (!match) return null;
  return { handle: match[1], postId: match[2] };
}

async function fetchPostData(handle: string, postId: string): Promise<BlueskyPostData> {
  // 1. ハンドルからDIDを解決
  const resolveResp = await fetch(
    `https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle=${encodeURIComponent(handle)}`
  );
  if (!resolveResp.ok) {
    throw new Error(`Failed to resolve handle: ${resolveResp.status}`);
  }
  const resolveData = await resolveResp.json();
  const did = resolveData.did;

  // 2. 投稿スレッド取得
  const uri = `at://${did}/app.bsky.feed.post/${postId}`;
  const threadResp = await fetch(
    `https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?uri=${encodeURIComponent(uri)}&depth=0`
  );
  if (!threadResp.ok) {
    throw new Error(`Failed to fetch post: ${threadResp.status}`);
  }
  const threadData = await threadResp.json();
  const post: BskyPost = threadData.thread.post;

  // 3. 画像URL抽出
  const imageUrls: string[] = [];
  if (post.embed) {
    const extractImages = (embed: BskyEmbed) => {
      if (embed.images) {
        for (const img of embed.images) {
          if (img.fullsize) {
            imageUrls.push(img.fullsize);
          }
        }
      }
      // recordWithMedia の場合
      if (embed.media?.images) {
        for (const img of embed.media.images) {
          if (img.fullsize) {
            imageUrls.push(img.fullsize);
          }
        }
      }
    };
    extractImages(post.embed);
  }

  // 4. タグ抽出（ハッシュタグfacets）
  const tags: string[] = [];
  if (post.record.facets) {
    for (const facet of post.record.facets) {
      for (const feature of facet.features) {
        if (feature.$type === 'app.bsky.richtext.facet#tag' && feature.tag) {
          tags.push(feature.tag);
        }
      }
    }
  }

  // 5. センシティブ判定（投稿ラベル + 著者ラベル + record内ラベル）
  let sensitive = false;
  const allLabels = [
    ...(post.labels || []),
    ...(post.author.labels || []),
    ...(post.record.labels?.values || []),
  ];
  if (allLabels.some(label => SENSITIVE_LABELS.has(label.val))) {
    sensitive = true;
  }

  return {
    postId,
    handle: post.author.handle,
    displayName: post.author.displayName || post.author.handle,
    text: post.record.text || '',
    imageUrls,
    sensitive,
    tags,
    sourceUrl: `https://bsky.app/profile/${post.author.handle}/post/${postId}`,
  };
}

// ===== 単独投稿ページ用フローティングボタン =====

let currentPostId: string | null = null;

function showFloatingButton() {
  const postInfo = extractPostInfo();
  if (!postInfo) {
    const existing = document.getElementById('hydrus-bsky-btn');
    if (existing) existing.style.display = 'none';
    return;
  }

  let btn = document.getElementById('hydrus-bsky-btn') as HTMLButtonElement | null;

  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'hydrus-bsky-btn';
    btn.textContent = 'H Import';
    btn.title = 'Import to Hydrus Network';

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
      background: '#0085ff',
      color: '#fff',
      fontSize: '14px',
      fontWeight: '700',
      cursor: 'pointer',
      transition: 'all 0.2s',
      boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
      whiteSpace: 'nowrap',
    } as Record<string, string>);

    btn.addEventListener('mouseenter', () => {
      if (btn!.dataset.state !== 'working') {
        btn!.style.background = '#0066cc';
      }
    });
    btn.addEventListener('mouseleave', () => {
      if (btn!.dataset.state !== 'working') {
        btn!.style.background = '#0085ff';
      }
    });

    document.body.appendChild(btn);
    btn.addEventListener('click', () => {
      const info = extractPostInfo();
      if (info) handleFloatingImport(btn!, info.handle, info.postId);
    });
  }

  if (currentPostId !== postInfo.postId) {
    currentPostId = postInfo.postId;
    btn.textContent = 'H Import';
    btn.style.background = '#0085ff';
    btn.disabled = false;
    btn.dataset.state = 'idle';
  }

  btn.style.display = 'flex';
}

async function handleFloatingImport(btn: HTMLButtonElement, handle: string, postId: string) {
  if (btn.dataset.state === 'working') return;
  if (btn.dataset.state === 'done') return;

  btn.textContent = 'Fetching...';
  btn.style.background = '#f5a623';
  btn.disabled = true;
  btn.dataset.state = 'working';

  try {
    const postData = await fetchPostData(handle, postId);
    if (postData.imageUrls.length === 0) {
      btn.textContent = 'No images';
      btn.style.background = '#e74c3c';
      btn.disabled = false;
      btn.dataset.state = 'error';
      return;
    }

    btn.textContent = `Importing... (0/${postData.imageUrls.length})`;

    const response = await sendToBackground({
      type: 'IMPORT_BLUESKY_POST',
      data: postData,
    });

    if (response.success) {
      const { imported, total } = response.data;
      btn.textContent = `Done (${imported}/${total})`;
      btn.style.background = '#2ecc71';
      btn.disabled = false;
      btn.dataset.state = 'done';
    } else {
      btn.textContent = 'Error';
      btn.style.background = '#e74c3c';
      btn.disabled = false;
      btn.dataset.state = 'error';
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    btn.textContent = 'Error';
    btn.style.background = '#e74c3c';
    btn.disabled = false;
    btn.dataset.state = 'error';
    console.error('[Hydrus Importer] Bluesky import error:', msg);
  }
}

// ===== タイムライン用インラインボタン =====

function findPostArticles(): Element[] {
  // Blueskyのタイムライン投稿要素を探す
  // bsky.appのReact DOMでは data-testid="feedItem-by-*" や投稿リンクで識別
  return Array.from(document.querySelectorAll('[data-testid^="feedItem-"], [data-testid="postThreadItem"]'));
}

function extractPostInfoFromElement(element: Element): { handle: string; postId: string } | null {
  // 投稿内のリンクからhandle/postIdを抽出
  const links = element.querySelectorAll('a[href*="/post/"]');
  for (const link of links) {
    const href = link.getAttribute('href') || '';
    const match = href.match(/\/profile\/([^/]+)\/post\/([^/]+)/);
    if (match) {
      return { handle: match[1], postId: match[2] };
    }
  }
  return null;
}

function hasImages(element: Element): boolean {
  // 画像を含む投稿かチェック
  // bsky.appでは画像は img[src*="cdn.bsky.app"] で見つかる
  const images = element.querySelectorAll('img[src*="cdn.bsky.app/img/feed"]');
  return images.length > 0;
}

function injectTimelineButtons() {
  const articles = findPostArticles();

  for (const article of articles) {
    if (article.getAttribute(PROCESSED_ATTR)) continue;
    if (!hasImages(article)) continue;

    const postInfo = extractPostInfoFromElement(article);
    if (!postInfo) continue;

    // アクションバーを探す（いいね・リポスト等のボタン行）
    // bsky.appでは role="button" が並ぶコンテナ
    const actionBars = article.querySelectorAll('[data-testid="replyBtn"], [data-testid="likeBtn"]');
    if (actionBars.length === 0) continue;

    // 最も近い親のアクションバーコンテナを取得
    const actionBar = actionBars[0].parentElement;
    if (!actionBar) continue;

    article.setAttribute(PROCESSED_ATTR, 'true');

    const btn = createPostButton();
    actionBar.appendChild(btn);

    const capturedInfo = { ...postInfo };
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      handleInlineImport(btn, capturedInfo.handle, capturedInfo.postId);
    });
  }
}

async function handleInlineImport(btn: HTMLButtonElement, handle: string, postId: string) {
  if (btn.dataset.state === 'working') return;
  if (btn.dataset.state === 'done') return;

  updatePostButtonState(btn, 'working');

  try {
    const postData = await fetchPostData(handle, postId);
    if (postData.imageUrls.length === 0) {
      updatePostButtonState(btn, 'error', 'No images');
      return;
    }

    const response = await sendToBackground({
      type: 'IMPORT_BLUESKY_POST',
      data: postData,
    });

    if (response.success) {
      const { imported, total } = response.data;
      updatePostButtonState(btn, 'done', `Imported ${imported}/${total}`);
    } else {
      updatePostButtonState(btn, 'error', response.error || 'Import failed');
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    updatePostButtonState(btn, 'error', msg);
    console.error('[Hydrus Importer] Bluesky import error:', msg);
  }
}

// ===== 進捗受信 =====

chrome.runtime.onMessage.addListener((message: { type: string; progress?: ImportProgress }) => {
  if (message.type === 'IMPORT_PROGRESS' && message.progress) {
    const progress = message.progress;

    // 単独投稿ページのフローティングボタン
    const floatingBtn = document.getElementById('hydrus-bsky-btn') as HTMLButtonElement | null;
    if (floatingBtn && floatingBtn.dataset.state === 'working') {
      switch (progress.phase) {
        case 'downloading':
          floatingBtn.textContent = `DL ${progress.current}/${progress.total}`;
          break;
        case 'importing':
          floatingBtn.textContent = `Import ${progress.current}/${progress.total}`;
          break;
        case 'tagging':
          floatingBtn.textContent = `Tag ${progress.current}/${progress.total}`;
          break;
      }
    }
  }
});

// ===== SPA ナビゲーション対応 =====

let lastUrl = location.href;

const observer = new MutationObserver(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href;
    setTimeout(() => {
      showFloatingButton();
      injectTimelineButtons();
    }, 300);
  } else {
    // 同一URL内でもDOMが変わる（タイムラインスクロール等）
    injectTimelineButtons();
  }
});

observer.observe(document.body, { childList: true, subtree: true });

// 初期表示
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
      showFloatingButton();
      injectTimelineButtons();
    }, 500);
  });
} else {
  setTimeout(() => {
    showFloatingButton();
    injectTimelineButtons();
  }, 500);
}
