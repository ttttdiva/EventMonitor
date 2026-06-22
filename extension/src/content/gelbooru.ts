import { BooruPostData, ImportProgress } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

// ===== 定数 =====
const BUTTON_ID = 'hydrus-gelbooru-btn';

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
    background: '#006ffa',
    color: '#fff',
    fontSize: '14px',
    fontWeight: '700',
    cursor: 'pointer',
    transition: 'all 0.2s',
    boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
    whiteSpace: 'nowrap',
  } as Record<string, string>);

  btn.addEventListener('mouseenter', () => {
    if (btn.dataset.state !== 'working') btn.style.background = '#0055cc';
  });
  btn.addEventListener('mouseleave', () => {
    if (btn.dataset.state !== 'working') btn.style.background = '#006ffa';
  });

  return btn;
}

function updateButtonState(btn: HTMLButtonElement, state: ButtonState, detail?: string) {
  btn.dataset.state = state;
  switch (state) {
    case 'idle':
      btn.textContent = 'H Import';
      btn.style.background = '#006ffa';
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

// ===== Gelbooru API =====

function extractPostId(): string | null {
  const params = new URLSearchParams(window.location.search);
  const id = params.get('id');
  // ページタイプが post の view であること
  if (params.get('page') === 'post' && params.get('s') === 'view' && id) {
    return id;
  }
  return null;
}

interface GelbooruApiResponse {
  post: Array<{
    id: number;
    file_url: string;
    source: string;
    rating: string; // general, sensitive, questionable, explicit
    tags: string;
  }>;
}

interface GelbooruTagInfo {
  tag: string;
  type: string; // tag, artist, character, copyright, metadata
}

async function fetchPostData(postId: string): Promise<BooruPostData> {
  // 投稿データ取得
  const resp = await fetch(
    `https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&id=${postId}`
  );
  if (!resp.ok) {
    throw new Error(`Gelbooru API error: ${resp.status}`);
  }
  const data: GelbooruApiResponse = await resp.json();
  if (!data.post || data.post.length === 0) {
    throw new Error('Post not found');
  }
  const post = data.post[0];

  if (!post.file_url) {
    throw new Error('No image URL available');
  }

  // タグをカテゴリ別に取得
  const tagCategories = await fetchTagCategories(post.tags);

  // rating: general, sensitive, questionable, explicit
  const sensitive = post.rating === 'questionable' || post.rating === 'explicit';

  return {
    postId: String(post.id),
    platform: 'gelbooru',
    imageUrl: post.file_url,
    sourceUrl: post.source || '',
    pageUrl: `https://gelbooru.com/index.php?page=post&s=view&id=${postId}`,
    sensitive,
    tags: tagCategories,
  };
}

async function fetchTagCategories(
  tagsString: string
): Promise<BooruPostData['tags']> {
  const result: BooruPostData['tags'] = {
    artist: [],
    character: [],
    copyright: [],
    general: [],
    meta: [],
  };

  // タグ情報をAPI経由で取得してカテゴリ分類
  const tagNames = tagsString.split(/\s+/).filter(t => t);
  if (tagNames.length === 0) return result;

  // Gelbooru tag API: 一度に100タグまで
  const chunks: string[][] = [];
  for (let i = 0; i < tagNames.length; i += 100) {
    chunks.push(tagNames.slice(i, i + 100));
  }

  for (const chunk of chunks) {
    const names = chunk.map(t => encodeURIComponent(t)).join(' ');
    try {
      const resp = await fetch(
        `https://gelbooru.com/index.php?page=dapi&s=tag&q=index&json=1&names=${names}&limit=100`
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      const tags: GelbooruTagInfo[] = data.tag || [];

      // type: 0=tag(general), 1=artist, 3=copyright, 4=character, 5=metadata
      const typeMap: Record<string, keyof BooruPostData['tags']> = {
        '0': 'general',
        '1': 'artist',
        '3': 'copyright',
        '4': 'character',
        '5': 'meta',
      };

      const categorized = new Set<string>();
      for (const tag of tags) {
        const category = typeMap[String((tag as any).type)];
        if (category && tag.tag) {
          result[category].push(tag.tag);
          categorized.add(tag.tag);
        }
      }

      // API で取得できなかったタグは general へ
      for (const name of chunk) {
        if (!categorized.has(name)) {
          result.general.push(name);
        }
      }
    } catch {
      // API失敗時は全タグを general へ
      result.general.push(...chunk);
    }
  }

  return result;
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
    console.error('[Hydrus Importer] Gelbooru import error:', msg);
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
