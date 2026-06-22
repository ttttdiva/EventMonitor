import { BooruPostData, ImportProgress } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

// ===== 定数 =====
const BUTTON_ID = 'hydrus-danbooru-btn';

// ===== ボタン状態管理 =====
type ButtonState = 'idle' | 'working' | 'done' | 'error';

function createFloatingButton(): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.id = BUTTON_ID;
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
    background: '#0073ff',
    color: '#fff',
    fontSize: '14px',
    fontWeight: '700',
    cursor: 'pointer',
    transition: 'all 0.2s',
    boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
    whiteSpace: 'nowrap',
  } as Record<string, string>);

  btn.addEventListener('mouseenter', () => {
    if (btn.dataset.state !== 'working') btn.style.background = '#005acc';
  });
  btn.addEventListener('mouseleave', () => {
    if (btn.dataset.state !== 'working') btn.style.background = '#0073ff';
  });

  return btn;
}

function updateButtonState(btn: HTMLButtonElement, state: ButtonState, detail?: string) {
  btn.dataset.state = state;
  switch (state) {
    case 'idle':
      btn.textContent = 'H Import';
      btn.style.background = '#0073ff';
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

// ===== Danbooru API =====

function extractPostId(): string | null {
  const match = window.location.pathname.match(/\/posts\/(\d+)/);
  return match ? match[1] : null;
}

interface DanbooruApiPost {
  id: number;
  file_url?: string;
  large_file_url?: string;
  tag_string_artist: string;
  tag_string_character: string;
  tag_string_copyright: string;
  tag_string_general: string;
  tag_string_meta: string;
  rating: string; // g, s, q, e
  source: string;
}

async function fetchPostData(postId: string): Promise<BooruPostData> {
  const resp = await fetch(`https://danbooru.donmai.us/posts/${postId}.json`);
  if (!resp.ok) {
    throw new Error(`Danbooru API error: ${resp.status}`);
  }
  const data: DanbooruApiPost = await resp.json();

  const imageUrl = data.file_url || data.large_file_url;
  if (!imageUrl) {
    throw new Error('No image URL (may require login for restricted content)');
  }

  const splitTags = (s: string) => s ? s.split(/\s+/).filter(t => t) : [];

  // rating: g=general, s=sensitive, q=questionable, e=explicit
  const sensitive = data.rating === 'q' || data.rating === 'e';

  return {
    postId: String(data.id),
    platform: 'danbooru',
    imageUrl,
    sourceUrl: data.source || '',
    pageUrl: `https://danbooru.donmai.us/posts/${postId}`,
    sensitive,
    tags: {
      artist: splitTags(data.tag_string_artist),
      character: splitTags(data.tag_string_character),
      copyright: splitTags(data.tag_string_copyright),
      general: splitTags(data.tag_string_general),
      meta: splitTags(data.tag_string_meta),
    },
  };
}

// ===== ボタン管理 =====

let currentPostId: string | null = null;

function showButton() {
  const postId = extractPostId();
  if (!postId) {
    const existing = document.getElementById(BUTTON_ID);
    if (existing) existing.style.display = 'none';
    return;
  }

  let btn = document.getElementById(BUTTON_ID) as HTMLButtonElement | null;

  if (!btn) {
    btn = createFloatingButton();
    document.body.appendChild(btn);
    btn.addEventListener('click', () => {
      const id = extractPostId();
      if (id) handleImport(btn!, id);
    });
  }

  if (currentPostId !== postId) {
    currentPostId = postId;
    updateButtonState(btn, 'idle');
  }

  btn.style.display = 'flex';
}

// ===== インポート処理 =====

async function handleImport(btn: HTMLButtonElement, postId: string) {
  if (btn.dataset.state === 'working') return;
  if (btn.dataset.state === 'done') return;

  updateButtonState(btn, 'working', 'Fetching...');

  try {
    const postData = await fetchPostData(postId);

    updateButtonState(btn, 'working', 'Importing...');

    const response = await sendToBackground({
      type: 'IMPORT_BOORU_POST',
      data: postData,
    });

    if (response.success) {
      updateButtonState(btn, 'done', 'Done (1/1)');
    } else {
      updateButtonState(btn, 'error', `Error: ${response.error}`);
    }
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    updateButtonState(btn, 'error', 'Error');
    console.error('[Hydrus Importer] Danbooru import error:', msg);
  }
}

// ===== 進捗受信 =====

chrome.runtime.onMessage.addListener((message: { type: string; progress?: ImportProgress }) => {
  if (message.type === 'IMPORT_PROGRESS' && message.progress) {
    const btn = document.getElementById(BUTTON_ID) as HTMLButtonElement | null;
    if (!btn || btn.dataset.state !== 'working') return;
    const p = message.progress;
    switch (p.phase) {
      case 'downloading': updateButtonState(btn, 'working', 'Downloading...'); break;
      case 'importing': updateButtonState(btn, 'working', 'Importing...'); break;
      case 'tagging': updateButtonState(btn, 'working', 'Tagging...'); break;
    }
  }
});

// ===== 初期表示 =====

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(showButton, 300));
} else {
  setTimeout(showButton, 300);
}
