import { ExtensionSettings } from '../lib/types';
import { sendToBackground } from '../lib/message-protocol';

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

// DOM要素
const apiUrlInput = document.getElementById('apiUrl') as HTMLInputElement;
const accessKeyInput = document.getElementById('accessKey') as HTMLInputElement;
const toggleKeyBtn = document.getElementById('toggleKey') as HTMLButtonElement;
const serviceTwitterInput = document.getElementById('serviceTwitter') as HTMLInputElement;
const servicePixivInput = document.getElementById('servicePixiv') as HTMLInputElement;
const serviceBlueskyInput = document.getElementById('serviceBluesky') as HTMLInputElement;
const serviceDanbooruInput = document.getElementById('serviceDanbooru') as HTMLInputElement;
const serviceGelbooruInput = document.getElementById('serviceGelbooru') as HTMLInputElement;
const customTagsInput = document.getElementById('customTags') as HTMLTextAreaElement;
const testBtn = document.getElementById('testConnection') as HTMLButtonElement;
const saveBtn = document.getElementById('save') as HTMLButtonElement;
const statusEl = document.getElementById('status') as HTMLDivElement;

function showStatus(message: string, type: 'success' | 'error' | 'info') {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
  statusEl.classList.remove('hidden');
  setTimeout(() => statusEl.classList.add('hidden'), 3000);
}

async function loadSettings(): Promise<ExtensionSettings> {
  const stored = await chrome.storage.local.get('settings');
  const s = (stored.settings || {}) as Partial<ExtensionSettings>;
  return normalizeSettings(s);
}

async function populateForm() {
  const settings = await loadSettings();
  apiUrlInput.value = settings.hydrusApiUrl;
  accessKeyInput.value = settings.hydrusAccessKey;
  serviceTwitterInput.value = settings.tagServices.twitter;
  servicePixivInput.value = settings.tagServices.pixiv;
  serviceBlueskyInput.value = settings.tagServices.bluesky;
  serviceDanbooruInput.value = settings.tagServices.danbooru;
  serviceGelbooruInput.value = settings.tagServices.gelbooru;
  customTagsInput.value = settings.customTags.join(', ');
}

function collectSettings(): ExtensionSettings {
  return {
    hydrusApiUrl: apiUrlInput.value.trim() || DEFAULT_SETTINGS.hydrusApiUrl,
    hydrusAccessKey: accessKeyInput.value.trim(),
    tagServices: {
      twitter: serviceTwitterInput.value.trim() || DEFAULT_SETTINGS.tagServices.twitter,
      pixiv: servicePixivInput.value.trim() || DEFAULT_SETTINGS.tagServices.pixiv,
      bluesky: serviceBlueskyInput.value.trim() || DEFAULT_SETTINGS.tagServices.bluesky,
      danbooru: serviceDanbooruInput.value.trim() || DEFAULT_SETTINGS.tagServices.danbooru,
      gelbooru: serviceGelbooruInput.value.trim() || DEFAULT_SETTINGS.tagServices.gelbooru,
    },
    customTags: customTagsInput.value
      .split(',')
      .map(t => t.trim())
      .filter(t => t.length > 0),
  };
}

// Access Key 表示切替
toggleKeyBtn.addEventListener('click', () => {
  accessKeyInput.type = accessKeyInput.type === 'password' ? 'text' : 'password';
});

// 保存
saveBtn.addEventListener('click', async () => {
  const settings = collectSettings();
  await chrome.storage.local.set({ settings });
  showStatus('Settings saved', 'success');
});

// 接続テスト
testBtn.addEventListener('click', async () => {
  const settings = collectSettings();
  // 一時保存して接続テスト
  await chrome.storage.local.set({ settings });
  showStatus('Connecting...', 'info');

  const response = await sendToBackground({ type: 'CHECK_HYDRUS_CONNECTION' });
  if (response.success && response.data?.ok) {
    showStatus(`Connected (API v${response.data.version})`, 'success');
  } else {
    showStatus(`Failed: ${response.data?.error || response.error}`, 'error');
  }
});

// 初期化
populateForm();
