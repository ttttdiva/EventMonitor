import { ExtensionMessage, MessageResponse } from './types';

/** コンテンツスクリプトからBackgroundへメッセージ送信 */
export function sendToBackground(message: ExtensionMessage): Promise<MessageResponse> {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (response: MessageResponse) => {
        if (chrome.runtime.lastError) {
          resolve({ success: false, error: chrome.runtime.lastError.message });
        } else {
          resolve(response || { success: false, error: 'No response' });
        }
      });
    } catch (e: unknown) {
      resolve({ success: false, error: e instanceof Error ? e.message : String(e) });
    }
  });
}

/** Backgroundから特定タブへ進捗通知 */
export function sendProgressToTab(tabId: number, progress: any): void {
  chrome.tabs.sendMessage(tabId, { type: 'IMPORT_PROGRESS', progress }).catch(() => {
    // タブが閉じられた場合は無視
  });
}
