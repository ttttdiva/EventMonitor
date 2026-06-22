// ===== 設定 =====
export interface ExtensionSettings {
  hydrusApiUrl: string;
  hydrusAccessKey: string;
  tagServices: {
    twitter: string;
    pixiv: string;
    bluesky: string;
    danbooru: string;
    gelbooru: string;
  };
  customTags: string[];
}

// ===== Pixiv データ =====
export interface PixivWorkData {
  id: string;
  title: string;
  userName: string;
  userId: string;
  tags: string[];
  xRestrict: number;
  pageCount: number;
  imageUrls: string[];
  sourceUrl: string;
}

// ===== Twitter データ =====
export interface TwitterTweetData {
  tweetId: string;
  username: string;
  displayName: string;
  text: string;
  imageUrls: string[];
  sensitive: boolean;
  sensitiveFlags: string[];
  sourceUrl: string;
}

// ===== Bluesky データ =====
export interface BlueskyPostData {
  postId: string;
  handle: string;
  displayName: string;
  text: string;
  imageUrls: string[];
  sensitive: boolean;
  tags: string[];
  sourceUrl: string;
}

// ===== Booru データ (Danbooru / Gelbooru 共通) =====
export interface BooruPostData {
  postId: string;
  platform: 'danbooru' | 'gelbooru';
  imageUrl: string;
  sourceUrl: string;
  pageUrl: string;
  sensitive: boolean;
  tags: {
    artist: string[];
    character: string[];
    copyright: string[];
    general: string[];
    meta: string[];
  };
}

// ===== インポート結果 =====
export interface ImportResult {
  success: boolean;
  hash?: string;
  error?: string;
  status?: number;
}

// ===== 進捗 =====
export interface ImportProgress {
  current: number;
  total: number;
  phase: 'downloading' | 'importing' | 'tagging' | 'done' | 'error';
  errorMessage?: string;
}

// ===== メッセージプロトコル =====
export interface ImportPixivWorkMessage {
  type: 'IMPORT_PIXIV_WORK';
  data: PixivWorkData;
  /** Downloaded in the Pixiv content script, preserving the old fast path for URLs that fit in one message. */
  imageDataList?: string[];
}

export interface ImportPixivImageChunkedStartMessage {
  type: 'IMPORT_PIXIV_IMAGE_CHUNKED_START';
  transferId: string;
  data: PixivWorkData;
  totalChunks: number;
}

export interface ImportPixivImageChunkMessage {
  type: 'IMPORT_PIXIV_IMAGE_CHUNK';
  transferId: string;
  chunkIndex: number;
  chunk: string;
}

export interface ImportPixivImageChunkedFinishMessage {
  type: 'IMPORT_PIXIV_IMAGE_CHUNKED_FINISH';
  transferId: string;
}

export interface ImportPixivImageChunkedAbortMessage {
  type: 'IMPORT_PIXIV_IMAGE_CHUNKED_ABORT';
  transferId: string;
}

export interface ImportTwitterTweetMessage {
  type: 'IMPORT_TWITTER_TWEET';
  data: TwitterTweetData;
  /** コンテンツスクリプトでDL済みの画像バイナリ (Base64) */
  imageDataList?: string[];
}

export interface ImportBlueskyPostMessage {
  type: 'IMPORT_BLUESKY_POST';
  data: BlueskyPostData;
}

export interface ImportBooruPostMessage {
  type: 'IMPORT_BOORU_POST';
  data: BooruPostData;
}

export interface CheckConnectionMessage {
  type: 'CHECK_HYDRUS_CONNECTION';
}

export interface GetSettingsMessage {
  type: 'GET_SETTINGS';
}

export interface ImportProgressMessage {
  type: 'IMPORT_PROGRESS';
  progress: ImportProgress;
}

export type ExtensionMessage =
  | ImportPixivWorkMessage
  | ImportPixivImageChunkedStartMessage
  | ImportPixivImageChunkMessage
  | ImportPixivImageChunkedFinishMessage
  | ImportPixivImageChunkedAbortMessage
  | ImportTwitterTweetMessage
  | ImportBlueskyPostMessage
  | ImportBooruPostMessage
  | CheckConnectionMessage
  | GetSettingsMessage
  | ImportProgressMessage;

export interface MessageResponse {
  success: boolean;
  data?: any;
  error?: string;
}
