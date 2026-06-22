import {
  ExtensionSettings,
  ExtensionMessage,
  MessageResponse,
  PixivWorkData,
  TwitterTweetData,
  BlueskyPostData,
  BooruPostData,
} from './lib/types';
import { HydrusApi } from './lib/hydrus-api';
import { generatePixivTags, generateTwitterTags, generateBlueskyTags, generateBooruTags, cleanTweetTextForNote } from './lib/tag-generator';
import { sendProgressToTab } from './lib/message-protocol';

const PIXIV_IMPORT_CONCURRENCY = 1;

type PixivChunkedTransfer = {
  work: PixivWorkData;
  chunks: string[];
  receivedChunks: number;
  totalChunks: number;
  tabId?: number;
};

const pixivChunkedTransfers = new Map<string, PixivChunkedTransfer>();

// ===== 設定管理 =====

const DEFAULT_SETTINGS: ExtensionSettings = {
  hydrusApiUrl: 'http://127.0.0.1:45869',
  hydrusAccessKey: '',
  tagServices: {
    twitter: 'my tags',
    pixiv: 'my tags',
    bluesky: 'my tags',
    danbooru: 'danbooru tags',
    gelbooru: 'danbooru tags',
  },
  customTags: [],
};

const LEGACY_TAG_SERVICE_DEFAULTS: Record<string, string> = {
  'twitter tags': 'my tags',
  'pixiv tags': 'my tags',
  'bluesky tags': 'my tags',
  'gelbooru tags': 'danbooru tags',
};

function normalizeServiceName(value: string | undefined, fallback: string): string {
  const serviceName = (value || '').trim();
  if (!serviceName) return fallback;
  return LEGACY_TAG_SERVICE_DEFAULTS[serviceName] || serviceName;
}

function normalizeSettings(stored: Partial<ExtensionSettings>): ExtensionSettings {
  const tagServices = (stored.tagServices || {}) as Partial<ExtensionSettings['tagServices']>;
  return {
    ...DEFAULT_SETTINGS,
    ...stored,
    tagServices: {
      twitter: normalizeServiceName(tagServices.twitter, DEFAULT_SETTINGS.tagServices.twitter),
      pixiv: normalizeServiceName(tagServices.pixiv, DEFAULT_SETTINGS.tagServices.pixiv),
      bluesky: normalizeServiceName(tagServices.bluesky, DEFAULT_SETTINGS.tagServices.bluesky),
      danbooru: normalizeServiceName(tagServices.danbooru, DEFAULT_SETTINGS.tagServices.danbooru),
      gelbooru: normalizeServiceName(tagServices.gelbooru, DEFAULT_SETTINGS.tagServices.gelbooru),
    },
    customTags: Array.isArray(stored.customTags) ? stored.customTags : DEFAULT_SETTINGS.customTags,
  };
}

async function loadSettings(): Promise<ExtensionSettings> {
  const stored = await chrome.storage.local.get('settings');
  const s = (stored.settings || {}) as Partial<ExtensionSettings>;
  return normalizeSettings(s);
}

// ===== 画像ダウンロード (リトライ付き) =====

async function downloadImage(
  url: string,
  headers?: Record<string, string>,
  maxRetries = 3,
  referrer?: string
): Promise<ArrayBuffer> {
  let lastError: Error | null = null;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      const resp = await fetch(url, { headers, referrer });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status} for ${url}`);
      }
      return await resp.arrayBuffer();
    } catch (e: unknown) {
      lastError = e instanceof Error ? e : new Error(String(e));
      if (attempt < maxRetries - 1) {
        await new Promise(r => setTimeout(r, 1000 * (attempt + 1)));
      }
    }
  }
  throw lastError || new Error(`Failed to download: ${url}`);
}

async function downloadPixivImage(url: string): Promise<ArrayBuffer> {
  try {
    return await downloadImage(url, { Referer: 'https://www.pixiv.net/' });
  } catch (headerError) {
    try {
      return await downloadImage(url, undefined, 3, 'https://www.pixiv.net/');
    } catch (referrerError) {
      const headerMessage = headerError instanceof Error ? headerError.message : String(headerError);
      const referrerMessage = referrerError instanceof Error ? referrerError.message : String(referrerError);
      throw new Error(`${headerMessage}; referrer retry: ${referrerMessage}`);
    }
  }
}

// ===== Base64 → ArrayBuffer 変換 =====

function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binaryStr = atob(base64);
  const bytes = new Uint8Array(binaryStr.length);
  for (let i = 0; i < binaryStr.length; i++) {
    bytes[i] = binaryStr.charCodeAt(i);
  }
  return bytes.buffer;
}

// ===== Pixivインポート =====

async function importPixivWork(
  work: PixivWorkData,
  settings: ExtensionSettings,
  tabId?: number,
  imageDataList?: string[]
): Promise<MessageResponse> {
  const api = new HydrusApi(settings);
  const tags = generatePixivTags(work, settings.customTags);
  const total = work.imageUrls.length;
  const hashes: string[] = [];
  const errors: string[] = [];
  let nextIndex = 0;
  let completed = 0;

  await api.resolveServices();

  async function importOne(i: number): Promise<void> {
    try {
      let bytes: ArrayBuffer;

      if (imageDataList) {
        const imageData = imageDataList[i];
        if (!imageData) {
          throw new Error(`Missing content-side image data for Pixiv image ${i + 1}`);
        }
        bytes = base64ToArrayBuffer(imageData);
      } else {
        const url = work.imageUrls[i];
        if (tabId) {
          sendProgressToTab(tabId, { current: i + 1, total, phase: 'downloading' });
        }
        bytes = await downloadPixivImage(url);
      }

      // Hydrusにインポート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'importing' });
      }
      const result = await api.importFile(bytes);
      if (!result.success || !result.hash) {
        throw new Error(result.error || 'Import failed');
      }

      // タグ付与 + URL紐付け + ノート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'tagging' });
      }
      if (!await api.addTags(result.hash, tags, settings.tagServices.pixiv)) {
        throw new Error(`Failed to add Pixiv tags via service "${settings.tagServices.pixiv}"`);
      }
      await api.associateUrl(result.hash, work.sourceUrl);

      // ノート: 全画像に付与 (Pythonクローラーと同一動作)
      if (work.title) {
        await api.addNote(result.hash, 'pixiv description', work.title);
      }

      hashes.push(result.hash);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      errors.push(`Image ${i + 1}: ${msg}`);
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'error', errorMessage: msg });
      }
    } finally {
      completed += 1;
    }
  }

  async function worker(): Promise<void> {
    while (nextIndex < total) {
      const i = nextIndex;
      nextIndex += 1;
      await importOne(i);
    }
  }

  const workerCount = Math.min(PIXIV_IMPORT_CONCURRENCY, total);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));

  // 完了通知
  if (tabId) {
    sendProgressToTab(tabId, { current: completed, total, phase: 'done' });
  }

  return {
    success: hashes.length > 0,
    error: hashes.length > 0 ? undefined : errors[0] || 'No Pixiv files imported',
    data: { hashes, imported: hashes.length, total, errors },
  };
}

function startPixivChunkedTransfer(
  transferId: string,
  work: PixivWorkData,
  totalChunks: number,
  tabId?: number
): MessageResponse {
  if (!transferId) {
    return { success: false, error: 'Missing Pixiv transfer ID' };
  }
  if (totalChunks <= 0) {
    return { success: false, error: 'Pixiv transfer has no chunks' };
  }

  pixivChunkedTransfers.set(transferId, {
    work,
    chunks: new Array(totalChunks),
    receivedChunks: 0,
    totalChunks,
    tabId,
  });
  return { success: true };
}

function appendPixivChunk(transferId: string, chunkIndex: number, chunk: string): MessageResponse {
  const transfer = pixivChunkedTransfers.get(transferId);
  if (!transfer) {
    return { success: false, error: 'Unknown Pixiv transfer ID' };
  }
  if (chunkIndex < 0 || chunkIndex >= transfer.totalChunks) {
    return { success: false, error: `Invalid Pixiv chunk index: ${chunkIndex}` };
  }
  if (transfer.chunks[chunkIndex] === undefined) {
    transfer.receivedChunks += 1;
  }
  transfer.chunks[chunkIndex] = chunk;
  return { success: true, data: { received: transfer.receivedChunks, total: transfer.totalChunks } };
}

async function finishPixivChunkedTransfer(
  transferId: string,
  settings: ExtensionSettings,
  fallbackTabId?: number
): Promise<MessageResponse> {
  const transfer = pixivChunkedTransfers.get(transferId);
  if (!transfer) {
    return { success: false, error: 'Unknown Pixiv transfer ID' };
  }
  if (transfer.receivedChunks !== transfer.totalChunks) {
    return {
      success: false,
      error: `Incomplete Pixiv transfer: ${transfer.receivedChunks}/${transfer.totalChunks} chunks`,
    };
  }

  pixivChunkedTransfers.delete(transferId);
  const imageData = transfer.chunks.join('');
  return await importPixivWork(transfer.work, settings, transfer.tabId ?? fallbackTabId, [imageData]);
}

function abortPixivChunkedTransfer(transferId: string): MessageResponse {
  pixivChunkedTransfers.delete(transferId);
  return { success: true };
}

// ===== Twitterインポート =====

async function importTwitterTweet(
  tweet: TwitterTweetData,
  settings: ExtensionSettings,
  tabId?: number,
  imageDataList?: string[]
): Promise<MessageResponse> {
  const api = new HydrusApi(settings);
  const tags = generateTwitterTags(tweet, settings.customTags);
  const noteText = cleanTweetTextForNote(tweet.text);
  const total = tweet.imageUrls.length;
  const hashes: string[] = [];
  const errors: string[] = [];

  for (let i = 0; i < total; i++) {
    const url = tweet.imageUrls[i];

    try {
      let bytes: ArrayBuffer;

      if (imageDataList && imageDataList[i]) {
        // コンテンツスクリプトでDL済み (Base64 → ArrayBuffer)
        bytes = base64ToArrayBuffer(imageDataList[i]);
      } else {
        // フォールバック: Background側でDL
        if (tabId) {
          sendProgressToTab(tabId, { current: i + 1, total, phase: 'downloading' });
        }
        bytes = await downloadImage(url);
      }

      // Hydrusにインポート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'importing' });
      }
      const result = await api.importFile(bytes);
      if (!result.success || !result.hash) {
        throw new Error(result.error || 'Import failed');
      }

      // タグ付与 + URL紐付け + ノート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'tagging' });
      }
      if (!await api.addTags(result.hash, tags, settings.tagServices.twitter)) {
        throw new Error(`Failed to add Twitter tags via service "${settings.tagServices.twitter}"`);
      }
      await api.associateUrl(result.hash, tweet.sourceUrl);

      if (noteText) {
        await api.addNote(result.hash, 'twitter description', noteText);
      }

      hashes.push(result.hash);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      errors.push(`Image ${i + 1}: ${msg}`);
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'error', errorMessage: msg });
      }
    }
  }

  if (tabId) {
    sendProgressToTab(tabId, { current: total, total, phase: 'done' });
  }

  return {
    success: hashes.length > 0,
    data: { hashes, imported: hashes.length, total, errors },
  };
}

// ===== Blueskyインポート =====

async function importBlueskyPost(
  post: BlueskyPostData,
  settings: ExtensionSettings,
  tabId?: number
): Promise<MessageResponse> {
  const api = new HydrusApi(settings);
  const tags = generateBlueskyTags(post, settings.customTags);
  const total = post.imageUrls.length;
  const hashes: string[] = [];
  const errors: string[] = [];

  for (let i = 0; i < total; i++) {
    const url = post.imageUrls[i];

    try {
      // ダウンロード (Bluesky CDNはReferer不要)
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'downloading' });
      }
      const bytes = await downloadImage(url);

      // Hydrusにインポート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'importing' });
      }
      const result = await api.importFile(bytes);
      if (!result.success || !result.hash) {
        throw new Error(result.error || 'Import failed');
      }

      // タグ付与 + URL紐付け + ノート
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'tagging' });
      }
      if (!await api.addTags(result.hash, tags, settings.tagServices.bluesky)) {
        throw new Error(`Failed to add Bluesky tags via service "${settings.tagServices.bluesky}"`);
      }
      await api.associateUrl(result.hash, post.sourceUrl);

      if (post.text) {
        await api.addNote(result.hash, 'bluesky post', post.text);
      }

      hashes.push(result.hash);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      errors.push(`Image ${i + 1}: ${msg}`);
      if (tabId) {
        sendProgressToTab(tabId, { current: i + 1, total, phase: 'error', errorMessage: msg });
      }
    }
  }

  if (tabId) {
    sendProgressToTab(tabId, { current: total, total, phase: 'done' });
  }

  return {
    success: hashes.length > 0,
    data: { hashes, imported: hashes.length, total, errors },
  };
}

// ===== Booruインポート (Danbooru / Gelbooru 共通) =====

async function importBooruPost(
  post: BooruPostData,
  settings: ExtensionSettings,
  tabId?: number
): Promise<MessageResponse> {
  const api = new HydrusApi(settings);
  const { myTags, booruTags } = generateBooruTags(post, settings.customTags);
  const booruServiceName = settings.tagServices[post.platform];

  try {
    // ダウンロード
    if (tabId) {
      sendProgressToTab(tabId, { current: 1, total: 1, phase: 'downloading' });
    }
    const bytes = await downloadImage(post.imageUrl);

    // Hydrusにインポート
    if (tabId) {
      sendProgressToTab(tabId, { current: 1, total: 1, phase: 'importing' });
    }
    const result = await api.importFile(bytes);
    if (!result.success || !result.hash) {
      throw new Error(result.error || 'Import failed');
    }

    // タグ付与 (管理タグ → my tags サービス, booruタグ → 専用サービス)
    if (tabId) {
      sendProgressToTab(tabId, { current: 1, total: 1, phase: 'tagging' });
    }
    // 管理タグは "my tags" サービスに付与（Python側と同一）
    if (!await api.addTags(result.hash, myTags, 'my tags')) {
      throw new Error('Failed to add Booru management tags via service "my tags"');
    }
    // booruタグは専用サービスに付与
    if (booruTags.length > 0) {
      if (!await api.addTags(result.hash, booruTags, booruServiceName)) {
        throw new Error(`Failed to add Booru tags via service "${booruServiceName}"`);
      }
    }
    // URL紐付け (投稿ページURL)
    await api.associateUrl(result.hash, post.pageUrl);
    // ソースURLも紐付け (pixiv等の元URL)
    if (post.sourceUrl && post.sourceUrl !== post.pageUrl) {
      await api.associateUrl(result.hash, post.sourceUrl);
    }

    if (tabId) {
      sendProgressToTab(tabId, { current: 1, total: 1, phase: 'done' });
    }

    return {
      success: true,
      data: { hashes: [result.hash], imported: 1, total: 1, errors: [] },
    };
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    if (tabId) {
      sendProgressToTab(tabId, { current: 1, total: 1, phase: 'error', errorMessage: msg });
    }
    return { success: false, error: msg };
  }
}

// ===== メッセージハンドラ =====

chrome.runtime.onMessage.addListener(
  (
    message: ExtensionMessage,
    sender: chrome.runtime.MessageSender,
    sendResponse: (response: MessageResponse) => void
  ) => {
    handleMessage(message, sender).then(sendResponse);
    return true; // 非同期レスポンスのために必須
  }
);

async function handleMessage(
  message: ExtensionMessage,
  sender: chrome.runtime.MessageSender
): Promise<MessageResponse> {
  switch (message.type) {
    case 'CHECK_HYDRUS_CONNECTION': {
      const settings = await loadSettings();
      const api = new HydrusApi(settings);
      const result = await api.checkConnection();
      return { success: result.ok, data: result };
    }

    case 'IMPORT_PIXIV_WORK': {
      const settings = await loadSettings();
      return await importPixivWork(message.data, settings, sender.tab?.id, message.imageDataList);
    }

    case 'IMPORT_PIXIV_IMAGE_CHUNKED_START': {
      return startPixivChunkedTransfer(
        message.transferId,
        message.data,
        message.totalChunks,
        sender.tab?.id
      );
    }

    case 'IMPORT_PIXIV_IMAGE_CHUNK': {
      return appendPixivChunk(message.transferId, message.chunkIndex, message.chunk);
    }

    case 'IMPORT_PIXIV_IMAGE_CHUNKED_FINISH': {
      const settings = await loadSettings();
      return await finishPixivChunkedTransfer(message.transferId, settings, sender.tab?.id);
    }

    case 'IMPORT_PIXIV_IMAGE_CHUNKED_ABORT': {
      return abortPixivChunkedTransfer(message.transferId);
    }

    case 'IMPORT_TWITTER_TWEET': {
      const settings = await loadSettings();
      return await importTwitterTweet(message.data, settings, sender.tab?.id, message.imageDataList);
    }

    case 'IMPORT_BLUESKY_POST': {
      const settings = await loadSettings();
      return await importBlueskyPost(message.data, settings, sender.tab?.id);
    }

    case 'IMPORT_BOORU_POST': {
      const settings = await loadSettings();
      return await importBooruPost(message.data, settings, sender.tab?.id);
    }

    case 'GET_SETTINGS': {
      const settings = await loadSettings();
      return { success: true, data: settings };
    }

    default:
      return { success: false, error: 'Unknown message type' };
  }
}
