import { PixivWorkData, TwitterTweetData, BlueskyPostData, BooruPostData } from './types';

/**
 * Pixivタグ生成 — hydrus_client.py _generate_pixiv_tags() と完全一致
 *
 * タグ構成:
 *   source:pixiv, imported_by:manual
 *   pixiv_id:{id}
 *   creator:{userName}
 *   title:{title} (100文字制限、超過時は97文字+"...")
 *   生Pixivタグ (そのまま)
 *   rating:r-18 (xRestrict >= 1)
 *   カスタムタグ
 */
export function generatePixivTags(work: PixivWorkData, customTags: string[]): string[] {
  const tags: string[] = [];

  // ベースタグ (Pythonではbase_tagsのsource:twitterをsource:pixivに置換)
  tags.push('source:pixiv');
  tags.push('imported_by:manual');

  // 作品IDタグ
  if (work.id) {
    tags.push(`pixiv_id:${work.id}`);
  }

  // クリエイタータグ
  if (work.userName) {
    tags.push(`creator:${work.userName}`);
  }

  // タイトルタグ (100文字制限)
  if (work.title) {
    let title = work.title;
    if (title.length > 100) {
      title = title.substring(0, 97) + '...';
    }
    tags.push(`title:${title}`);
  }

  // 生Pixivタグ (そのまま追加)
  for (const tag of work.tags) {
    if (tag) {
      tags.push(tag);
    }
  }

  // センシティブ判定
  if (work.xRestrict >= 1) {
    tags.push('rating:r-18');
  }

  // カスタムタグ
  for (const tag of customTags) {
    if (tag) {
      tags.push(tag);
    }
  }

  // 重複除去 (Pythonと同一: list(set(tags)))
  return [...new Set(tags)];
}

/**
 * Twitterタグ生成 — hydrus_client.py _generate_tags() と完全一致
 *
 * タグ構成:
 *   source:twitter, imported_by:manual
 *   tweet_id:{id} (数字のみ)
 *   creator:{displayName}
 *   twitter_user:{username}
 *   title:{1行目} (t.co除去, タブ→空白, 空白圧縮, 100文字制限)
 *   rating:r-18 (sensitive or sensitiveFlags非空)
 *   カスタムタグ
 */
export function generateTwitterTags(tweet: TwitterTweetData, customTags: string[]): string[] {
  const tags: string[] = [];

  // ベースタグ
  tags.push('source:twitter');
  tags.push('imported_by:manual');

  // ツイートIDタグ (数字のみ抽出 — Python: re.sub(r'\D', '', str(id)))
  if (tweet.tweetId) {
    const digitsOnly = tweet.tweetId.replace(/\D/g, '');
    if (digitsOnly) {
      tags.push(`tweet_id:${digitsOnly}`);
    }
  }

  // クリエイタータグ (displayNameのみcreator:に入れる)
  if (tweet.displayName) {
    tags.push(`creator:${tweet.displayName}`);
  }
  // usernameはtwitter_user:タグとして追加（creator:とは分離）
  if (tweet.username) {
    tags.push(`twitter_user:${tweet.username}`);
  }

  // タイトルタグ (ツイート本文の1行目)
  if (tweet.text) {
    // t.coリンク除去 (Python: re.sub(r'https?://t\.co/\S+', '', ...))
    let cleaned = tweet.text.replace(/https?:\/\/t\.co\/\S+/g, '').trim();
    if (cleaned) {
      // 1行目のみ
      let firstLine = cleaned.split('\n')[0].trim();
      // タブ→空白
      firstLine = firstLine.replace(/\t/g, ' ');
      // 連続空白を圧縮
      firstLine = firstLine.replace(/\s+/g, ' ');
      // 100文字制限
      if (firstLine.length > 100) {
        firstLine = firstLine.substring(0, 97) + '...';
      }
      if (firstLine) {
        tags.push(`title:${firstLine}`);
      }
    }
  }

  // センシティブ判定
  if (tweet.sensitive || (tweet.sensitiveFlags && tweet.sensitiveFlags.length > 0)) {
    tags.push('rating:r-18');
  }

  // カスタムタグ
  for (const tag of customTags) {
    if (tag) {
      tags.push(tag);
    }
  }

  return [...new Set(tags)];
}

/**
 * Blueskyタグ生成 — hydrus_client.py _generate_bluesky_tags() と完全一致
 *
 * タグ構成:
 *   source:bluesky, imported_by:manual
 *   bluesky_id:{postId}
 *   creator:{displayName}
 *   title:{text} (100文字制限、超過時は97文字+"...")
 *   ハッシュタグ (そのまま)
 *   rating:r-18 (sensitive=true)
 *   カスタムタグ
 */
export function generateBlueskyTags(post: BlueskyPostData, customTags: string[]): string[] {
  const tags: string[] = [];

  // ベースタグ
  tags.push('source:bluesky');
  tags.push('imported_by:manual');

  // 投稿IDタグ
  if (post.postId) {
    tags.push(`bluesky_id:${post.postId}`);
  }

  // クリエイタータグ
  if (post.displayName) {
    tags.push(`creator:${post.displayName}`);
  }

  // タイトルタグ (投稿テキストの先頭100文字)
  if (post.text) {
    let title = post.text.split('\n')[0].trim();
    if (title.length > 100) {
      title = title.substring(0, 97) + '...';
    }
    if (title) {
      tags.push(`title:${title}`);
    }
  }

  // ハッシュタグ (そのまま追加)
  for (const tag of post.tags) {
    if (tag) {
      tags.push(tag);
    }
  }

  // センシティブ判定
  if (post.sensitive) {
    tags.push('rating:r-18');
  }

  // カスタムタグ
  for (const tag of customTags) {
    if (tag) {
      tags.push(tag);
    }
  }

  return [...new Set(tags)];
}

/**
 * Booruタグ生成 — hydrus_client.py _generate_gelbooru_tags_split() と同等
 *
 * Python側と同様に管理タグ(myTags)とdanbooruタグ体系(booruTags)に分離:
 *   myTags: source:{platform}, imported_by:manual, {platform}_id:{id}, creator:{artist}, {platform}_artist:{artist}, rating:r-18
 *   booruTags: artist(raw), character:{name}, series:{copyright}, general(raw), meta(raw)
 */
export function generateBooruTags(
  post: BooruPostData,
  customTags: string[]
): { myTags: string[]; booruTags: string[] } {
  const myTags: string[] = [];
  const booruTags: string[] = [];

  // === myTags: EventMonitor管理タグ ===
  myTags.push(`source:${post.platform}`);
  myTags.push('imported_by:manual');

  if (post.postId) {
    myTags.push(`${post.platform}_id:${post.postId}`);
  }

  for (const artist of post.tags.artist) {
    if (artist) {
      myTags.push(`creator:${artist}`);
      myTags.push(`${post.platform}_artist:${artist}`);
    }
  }

  if (post.sensitive) {
    myTags.push('rating:r-18');
  }

  for (const tag of customTags) {
    if (tag) myTags.push(tag);
  }

  // === booruTags: danbooruタグ体系 ===
  for (const artist of post.tags.artist) {
    if (artist) booruTags.push(artist);
  }

  for (const char of post.tags.character) {
    if (char) booruTags.push(`character:${char}`);
  }

  for (const cr of post.tags.copyright) {
    if (cr) booruTags.push(`series:${cr}`);
  }

  for (const tag of post.tags.general) {
    if (tag) booruTags.push(tag);
  }

  for (const tag of post.tags.meta) {
    if (tag) booruTags.push(tag);
  }

  return {
    myTags: [...new Set(myTags)],
    booruTags: [...new Set(booruTags)],
  };
}

/**
 * Twitterノート用のテキストクリーニング
 * Python: import_tweet_images() 内のテキスト処理と一致
 *   - t.coリンク除去
 *   - タブ→空白
 *   - 空行除去
 */
export function cleanTweetTextForNote(text: string): string {
  if (!text) return '';
  let cleaned = text.trim();
  cleaned = cleaned.replace(/\t/g, ' ');
  cleaned = cleaned.replace(/https?:\/\/t\.co\/\S+/g, '');
  const lines = cleaned.split('\n').map(l => l.trim()).filter(l => l.length > 0);
  return lines.join('\n');
}
