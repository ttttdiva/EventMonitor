import { ImportResult, ExtensionSettings } from './types';

export class HydrusApi {
  private apiUrl: string;
  private accessKey: string;
  // サービス名→検証済みかどうかのキャッシュ
  private validatedServices: Map<string, boolean> = new Map();
  private availableLocalServices: string[] = [];
  private servicesResolved = false;

  constructor(settings: ExtensionSettings) {
    this.apiUrl = settings.hydrusApiUrl.replace(/\/+$/, '');
    this.accessKey = settings.hydrusAccessKey;
  }

  private headers(contentType?: string): Record<string, string> {
    const h: Record<string, string> = {
      'Hydrus-Client-API-Access-Key': this.accessKey,
    };
    if (contentType) h['Content-Type'] = contentType;
    return h;
  }

  /** 接続確認: APIバージョンを取得 */
  async checkConnection(): Promise<{ ok: boolean; version?: number; error?: string }> {
    try {
      const resp = await fetch(`${this.apiUrl}/api_version`, {
        headers: this.headers(),
      });
      if (!resp.ok) {
        return { ok: false, error: `Hydrus api_version HTTP ${resp.status}` };
      }
      const data = await resp.json();
      const verifyResp = await fetch(`${this.apiUrl}/verify_access_key`, {
        headers: this.headers(),
      });
      if (!verifyResp.ok) {
        const errText = await verifyResp.text().catch(() => '');
        const detail = errText ? `: ${errText}` : '';
        return { ok: false, version: data.version, error: `Hydrus access key HTTP ${verifyResp.status}${detail}` };
      }
      return { ok: true, version: data.version };
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      return { ok: false, error: msg };
    }
  }

  /**
   * Hydrusの利用可能なタグサービス一覧を取得
   * Python: _validate_tag_services() と同等
   */
  async resolveServices(): Promise<void> {
    if (this.servicesResolved) return;
    try {
      const resp = await fetch(`${this.apiUrl}/get_services`, {
        headers: this.headers(),
      });
      if (!resp.ok) {
        console.warn('[Hydrus] get_services failed:', resp.status);
        this.servicesResolved = true;
        return;
      }
      const data = await resp.json();

      // local_tags と tag_repositories からサービス名を収集
      for (const category of ['local_tags', 'tag_repositories']) {
        const services = data[category];
        if (Array.isArray(services)) {
          for (const svc of services) {
            if (svc && typeof svc === 'object' && svc.name) {
              this.validatedServices.set(svc.name, true);
              if (category === 'local_tags') {
                this.availableLocalServices.push(svc.name);
              }
            }
          }
        }
      }

      console.log('[Hydrus] Available tag services:', [...this.validatedServices.keys()]);
      console.log('[Hydrus] Local tag services:', this.availableLocalServices);
    } catch (e) {
      console.warn('[Hydrus] Failed to resolve services:', e);
    }
    this.servicesResolved = true;
  }

  /**
   * 有効なサービス名を解決する。
   * 存在しない名前を別サービスへ黙って流すとタグ汚染になるため、ここでは失敗させる。
   */
  private resolveServiceName(configuredName: string): string | null {
    // 設定されたサービス名がHydrusに存在する場合
    if (this.validatedServices.has(configuredName)) {
      return configuredName;
    }
    console.error(
      `[Hydrus] Tag service "${configuredName}" not found. ` +
      `Available local services: ${this.availableLocalServices.join(', ')}`
    );
    return null;
  }

  /**
   * ファイルインポート (バイナリ送信)
   * Python: POST /add_files/add_file (application/octet-stream)
   */
  async importFile(fileBytes: ArrayBuffer): Promise<ImportResult> {
    try {
      const resp = await fetch(`${this.apiUrl}/add_files/add_file`, {
        method: 'POST',
        headers: this.headers('application/octet-stream'),
        body: fileBytes,
      });
      if (!resp.ok) {
        const errText = await resp.text().catch(() => '');
        const detail = errText ? `: ${errText}` : '';
        return { success: false, error: `Hydrus add_file HTTP ${resp.status}${detail}` };
      }
      const result = await resp.json();
      const status: number = result.status;
      // status 1=成功, 2=既存, 3=削除済み→復元
      if (status === 1 || status === 2 || status === 3) {
        return { success: true, hash: result.hash, status };
      }
      return { success: false, error: `Hydrus status: ${status} - ${result.note || ''}`, status };
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      return { success: false, error: msg };
    }
  }

  /**
   * タグ付与
   * サービス名を検証し、存在しなければ失敗させる
   */
  async addTags(hash: string, tags: string[], configuredServiceName: string): Promise<boolean> {
    try {
      // サービス解決がまだなら実行
      await this.resolveServices();

      const serviceName = this.resolveServiceName(configuredServiceName);
      if (!serviceName) {
        console.error('[Hydrus] Cannot add tags: no valid service found');
        return false;
      }

      console.log(`[Hydrus] Adding ${tags.length} tags to ${hash.substring(0, 8)}... via service "${serviceName}"`);
      console.log('[Hydrus] Tags:', tags);

      const resp = await fetch(`${this.apiUrl}/add_tags/add_tags`, {
        method: 'POST',
        headers: this.headers('application/json'),
        body: JSON.stringify({
          hashes: [hash],
          service_names_to_actions_to_tags: {
            [serviceName]: { '0': tags },
          },
          override_previously_deleted_mappings: true,
        }),
      });

      if (!resp.ok) {
        const errText = await resp.text().catch(() => '');
        console.error(`[Hydrus] addTags failed: HTTP ${resp.status}`, errText);
      }
      return resp.ok;
    } catch (e) {
      console.error('[Hydrus] addTags error:', e);
      return false;
    }
  }

  /**
   * URL紐付け
   * Python: POST /add_urls/associate_url { hash, url_to_add }
   */
  async associateUrl(hash: string, url: string): Promise<boolean> {
    try {
      const resp = await fetch(`${this.apiUrl}/add_urls/associate_url`, {
        method: 'POST',
        headers: this.headers('application/json'),
        body: JSON.stringify({ hash, url_to_add: url }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }

  /**
   * ノート追加
   * Python: POST /add_notes/set_notes { hash, notes: { name: text } }
   */
  async addNote(hash: string, noteName: string, noteText: string): Promise<boolean> {
    try {
      const resp = await fetch(`${this.apiUrl}/add_notes/set_notes`, {
        method: 'POST',
        headers: this.headers('application/json'),
        body: JSON.stringify({ hash, notes: { [noteName]: noteText } }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }
}
