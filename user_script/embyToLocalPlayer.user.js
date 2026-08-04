// ==UserScript==
// @name         embyToLocalPlayer
// @name:zh-CN   embyToLocalPlayer
// @name:en      embyToLocalPlayer
// @namespace    https://github.com/kjtsune/embyToLocalPlayer
// @version      2026.08.04.2
// @updateURL    https://raw.githubusercontent.com/hope140/embyToLocalPlayer/watch_together/user_script/embyToLocalPlayer.user.js
// @downloadURL  https://raw.githubusercontent.com/hope140/embyToLocalPlayer/watch_together/user_script/embyToLocalPlayer.user.js
// @description  Emby/Jellyfin 调用外部本地播放器，并回传播放记录。适配 Plex。
// @description:zh-CN Emby/Jellyfin 调用外部本地播放器，并回传播放记录。适配 Plex。
// @description:en  Play in an external player. Update watch history to Emby/Jellyfin server. Support Plex.
// @author       Kjtsune
// @match        *://*/web/index.html*
// @match        *://*/*/web/index.html*
// @match        *://*/web/
// @match        *://*/*/web/
// @match        https://app.emby.media/*
// @match        https://app.plex.tv/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=emby.media
// @grant        unsafeWindow
// @grant        GM_info
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @run-at       document-start
// @connect      127.0.0.1
// @license MIT
// ==/UserScript==
'use strict';
/*global ApiClient*/

(function () {
    'use strict';
    let fistTime = true;
    let config = {
        logLevel: 2,
        disableOpenFolder: undefined, // undefined 改为 true 则禁用打开文件夹的按钮。
        crackFullPath: undefined,
        disableForLiveTv: undefined, // undefined 改为 true 则在浏览器里播放 IPTV。
        enableResumeReorder: true, // true 改为 undefined 则禁用。继续观看的前2位不变, 余下近3天更新的前移。
        resumeHideSomeSeries: undefined, // undefined 改为 true 则启用隐藏特定电视剧的油猴功能菜单。
    };

    let etlpStorageKeys = {
        webPlayerEnable: 'webPlayerEnable',
        mountDiskEnable: 'mountDiskEnable',
        crackFullPath: 'etlpCrackFullPath',
        resumeHide: 'etlpResumeHideSomeSeries',
        cacheResumeIds: 'etlpCacheResumeIds',
        hideSeriesIds: 'etlpResumeHideSeriesIds',
    }

    const originFetch = fetch;

    let logger = {
        error: function (...args) {
            if (config.logLevel >= 1) {
                console.log('%cERROR', 'color: #fff; background: #d32f2f; font-weight: bold; padding: 2px 6px; border-radius: 3px;', ...args);
            }
        },
        info: function (...args) {
            if (config.logLevel >= 2) {
                console.log('%cINFO', 'color: #fff; background: #1976d2; font-weight: bold; padding: 2px 6px; border-radius: 3px;', ...args);
            }
        },
        debug: function (...args) {
            if (config.logLevel >= 3) {
                console.log('%cDEBUG', 'color: #333; background: #ffeb3b; font-weight: bold; padding: 2px 6px; border-radius: 3px;', ...args);
            }
        },
    };

    function myBool(value) {
        if (Array.isArray(value) && value.length === 0) return false;
        if (value !== null && typeof value === 'object' && Object.keys(value).length === 0) return false;
        return Boolean(value);
    }

    async function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function isHidden(el) {
        return (el.offsetParent === null);
    }

    function getVisibleElement(elList) {
        if (!elList) return;
        if (Object.prototype.isPrototypeOf.call(NodeList.prototype, elList)) {
            for (let i = 0; i < elList.length; i++) {
                if (!isHidden(elList[i])) {
                    return elList[i];
                }
            }
        } else {
            return elList;
        }
    }

    function overwriteConfByStore() {
        function overwriteByKey(confKey) {
            let confLocal = localStorage.getItem(confKey);
            if (confLocal == null) return;
            if (confLocal == 'true') {
                GM_setValue(confKey, true);

            } else if (confLocal == 'false') {
                GM_setValue(confKey, false);
            }
            let confGM = GM_getValue(confKey, null);
            if (confGM !== null) {
                // 注意：etlpResumeHideSomeSeries 转换为 resumeHideSomeSeries。
                let _confKey = confKey.replace(/^etlp/, '');
                _confKey = _confKey.charAt(0).toLowerCase() + _confKey.slice(1);
                config[_confKey] = confGM;
            };
        }
        overwriteByKey(etlpStorageKeys.crackFullPath);
        overwriteByKey(etlpStorageKeys.resumeHide);
    }

    function playNotifiy(title = '正在播放', subtitle = '开始享受您的内容') {
        if (!document.getElementById('play-notification-style')) {
            const style = document.createElement('style');
            style.id = 'play-notification-style';
            style.textContent = `
                @keyframes slideIn { from { transform: translateX(400px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
                @keyframes slideOut { from { transform: translateX(0); opacity: 1; } to { transform: translateX(400px); opacity: 0; } }
                @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
            `;
            document.head.appendChild(style);
        }

        const notification = document.createElement('div');
        notification.innerHTML = `
            <svg width="40" height="40" viewBox="0 0 24 24" style="animation: pulse 1.5s ease-in-out infinite; flex-shrink: 0;">
                <circle cx="12" cy="12" r="10" stroke="white" stroke-width="2" fill="none" opacity="0.3"/>
                <path d="M9 8L17 12L9 16V8Z" fill="white"/>
            </svg>
            <div>
                <div style="font-weight: 600; font-size: 16px;">${title}</div>
                <div style="font-size: 13px; opacity: 0.9;">${subtitle}</div>
            </div>
        `;

        notification.style.cssText = `
            position: fixed; bottom: 30px; right: 30px; z-index: 999999;
            background: linear-gradient(135deg, #0296beff 0%, #008a51ff 100%);
            border-radius: 12px; padding: 20px 25px; color: white;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
            display: flex; align-items: center; gap: 15px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            animation: slideIn 0.5s ease-out;
        `;

        document.body.appendChild(notification);

        setTimeout(() => {
            notification.style.animation = 'slideOut 0.5s ease-in';
            setTimeout(() => notification.remove(), 500);
        }, 3000);
    }

    let menuRegistry = [];
    let registeredMenus = [];

    function switchLocalStorage(key, defaultValue = 'true', trueValue = 'true', falseValue = 'false') {
        if (key in localStorage) {
            let value = (localStorage.getItem(key) === trueValue) ? falseValue : trueValue;
            localStorage.setItem(key, value);
        } else {
            localStorage.setItem(key, defaultValue);
        }
        logger.info('switchLocalStorage', key, 'to', localStorage.getItem(key));
    }

    function registerAllMenus() {
        registeredMenus.forEach(id => GM_unregisterMenuCommand(id));
        registeredMenus = [];

        menuRegistry.forEach(item => {
            let id;

            if (item.type === 'switch') {
                let title = item.menuStart + item.switchNameMap[localStorage.getItem(item.storageKey)] + item.menuEnd;
                id = GM_registerMenuCommand(title, () => {
                    switchLocalStorage(item.storageKey);
                    registerAllMenus(); // 刷新菜单显示
                });
            } else if (item.type === 'callback') {
                id = GM_registerMenuCommand(item.title, item.callback);
            }

            registeredMenus.push(id);
            item.menuId = id;
        });
    }

    function setModeSwitchMenu(storageKey, menuStart = '', menuEnd = '', defaultValue = '关闭', trueValue = '开启', falseValue = '关闭') {
        let switchNameMap = { 'true': trueValue, 'false': falseValue, null: defaultValue };

        menuRegistry.push({
            type: 'switch',
            storageKey,
            menuStart,
            menuEnd,
            switchNameMap
        });

        registerAllMenus();
    }

    function setCallbackMenu(title, callback) {
        menuRegistry.push({
            type: 'callback',
            title,
            callback
        });

        registerAllMenus();
    }

    // --- watch-together administrator UI (Emby only; credentials stay in memory) ---
    const WATCH_TOGETHER_BASE_URL = 'http://127.0.0.1:58000';
    const WATCH_TOGETHER_ENDPOINTS = {
        auth: '/watch-together/auth',
        list: '/watch-together/rooms/list',
        create: '/watch-together/rooms/create',
        action: '/watch-together/rooms/action',
        delete: '/watch-together/rooms/delete',
    };
    const WATCH_TOGETHER_TOKEN_HEADER = 'X-ETLP-Watch-Token';
    const WATCH_TOGETHER_TIMEOUT_MS = 10000;
    const WATCH_TOGETHER_NAV_ID = 'etlp-watch-together-nav';
    const WATCH_TOGETHER_PAGE_ID = 'etlp-watch-together-page';
    const WATCH_TOGETHER_NAV_DATA = 'data-etlp-watch-together-nav';
    let watchTogetherToken = null;
    let watchTogetherTokenGeneration = null;
    let watchTogetherActiveState = null;
    let watchTogetherSessionGeneration = 0;
    let watchTogetherNavigationObserver = null;
    let watchTogetherNavigationFrame = 0;
    let watchTogetherNavigationStarted = false;

    function getWatchTogetherApiClient() {
        try {
            if (typeof ApiClient !== 'undefined' && ApiClient) return ApiClient;
        } catch (_) {
            // ApiClient may not be defined on a non-Emby page.
        }
        try {
            if (typeof unsafeWindow !== 'undefined' && unsafeWindow.ApiClient) return unsafeWindow.ApiClient;
        } catch (_) {
            // Ignore access errors from the page bridge.
        }
        return null;
    }

    function normalizeWatchTogetherServerUrl(value) {
        if (!value) return null;
        try {
            const parsed = new URL(String(value), window.location.href);
            if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return null;
            if (parsed.username || parsed.password) return null;
            if (parsed.hostname.toLowerCase() === 'app.emby.media') return null;
            let path = parsed.pathname.replace(/\/+$/, '');
            path = path.replace(/\/web\/index\.html$/i, '').replace(/\/web$/i, '').replace(/\/emby$/i, '');
            return `${parsed.protocol}//${parsed.host}${path}`.replace(/\/$/, '');
        } catch (_) {
            return null;
        }
    }

    function watchTogetherReadValues(source, keys) {
        if (!source) return [];
        const values = [];
        for (const key of keys) {
            try {
                let value = source[key];
                if (typeof value === 'function') value = value.call(source);
                if (value && typeof value.then !== 'function') values.push(String(value));
            } catch (_) {
                // Continue with the remaining compatibility fields.
            }
        }
        return values;
    }

    function watchTogetherReadValue(source, keys) {
        return watchTogetherReadValues(source, keys)[0] || null;
    }

    function getWatchTogetherApiContext() {
        const client = getWatchTogetherApiClient();
        if (!client) {
            return { error: '未检测到 Emby ApiClient，请在 Emby 网页登录后刷新页面。' };
        }
        const appName = String(client._appName || client.appName || '').toLowerCase();
        if (appName.includes('jellyfin') || appName.includes('plex') || serverName === 'jellyfin' || serverName === 'plex') {
            return { error: '同步观看房间仅支持 Emby，不支持 Jellyfin 或 Plex。' };
        }
        if (!appName.includes('emby') && serverName !== 'emby') {
            return { error: '请在 Emby 网页中打开同步观看房间菜单。' };
        }

        let serverInfo = client._serverInfo || null;
        if (!serverInfo) {
            try {
                if (typeof client.serverInfo === 'function') serverInfo = client.serverInfo();
                else if (typeof client.serverInfo === 'object') serverInfo = client.serverInfo;
            } catch (_) {
                serverInfo = null;
            }
        }
        const serverCandidates = [
            ...watchTogetherReadValues(client, ['_serverAddress']),
            ...watchTogetherReadValues(client, ['serverAddress']),
            ...watchTogetherReadValues(serverInfo, ['Address', 'ServerUrl', 'ServerURL', 'Url', 'RemoteAddress', 'LocalAddress']),
            window.location.origin,
        ];
        const serverUrl = serverCandidates.map(normalizeWatchTogetherServerUrl).find(Boolean);
        const userId = watchTogetherReadValue(client, ['_userId', 'getCurrentUserId'])
            || watchTogetherReadValue(serverInfo, ['UserId', 'userId'])
            || watchTogetherReadValue(client._userAuthInfo, ['UserId', 'userId']);
        const accessToken = watchTogetherReadValue(client._userAuthInfo, ['AccessToken', 'accessToken'])
            || watchTogetherReadValue(serverInfo, ['AccessToken', 'accessToken'])
            || watchTogetherReadValue(client, ['_accessToken', 'accessToken']);
        if (!serverUrl || !userId || !accessToken) {
            return { error: '无法取得 Emby 服务器地址、用户或登录令牌，请重新登录并刷新页面。' };
        }
        return { client, serverUrl, userId, accessToken };
    }

    function makeWatchTogetherError(status, code, message) {
        const error = new Error(message || '同步观看请求失败');
        error.status = Number(status) || 0;
        error.code = code || '';
        return error;
    }

    function parseWatchTogetherResponse(response) {
        let payload = null;
        try {
            payload = response.responseText ? JSON.parse(response.responseText) : {};
        } catch (_) {
            payload = {};
        }
        const status = Number(response.status) || 0;
        if (status >= 200 && status < 300) return payload;
        const backendError = payload && payload.error;
        throw makeWatchTogetherError(
            status,
            backendError && backendError.code,
            backendError && backendError.message || `HTTP ${status || '请求失败'}`,
        );
    }

    function watchTogetherRequest(path, body = {}, token = null) {
        return new Promise((resolve, reject) => {
            const headers = { 'Content-Type': 'application/json' };
            if (token) headers[WATCH_TOGETHER_TOKEN_HEADER] = token;
            let settled = false;
            const finish = (callback, value) => {
                if (settled) return;
                settled = true;
                callback(value);
            };
            try {
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${WATCH_TOGETHER_BASE_URL}${path}`,
                    data: JSON.stringify(body || {}),
                    headers,
                    timeout: WATCH_TOGETHER_TIMEOUT_MS,
                    onload: response => {
                        try {
                            finish(resolve, parseWatchTogetherResponse(response));
                        } catch (error) {
                            finish(reject, error);
                        }
                    },
                    onerror: () => finish(reject, makeWatchTogetherError(0, 'network_error', '本地服务未运行或无法连接')),
                    ontimeout: () => finish(reject, makeWatchTogetherError(0, 'timeout', '请求本地服务超时')),
                    onabort: () => finish(reject, makeWatchTogetherError(0, 'aborted', '请求已取消')),
                });
            } catch (_) {
                finish(reject, makeWatchTogetherError(0, 'network_error', '无法调用本地服务'));
            }
        });
    }

    function watchTogetherStateIsCurrent(state, generation = state && state.generation) {
        if (!state) return true;
        return watchTogetherActiveState === state && !state.closed && state.generation === generation;
    }

    function watchTogetherInvalidateToken(state = null) {
        if (!state || watchTogetherTokenGeneration === state.generation) {
            watchTogetherToken = null;
            watchTogetherTokenGeneration = null;
        }
    }

    async function watchTogetherAuthenticate(context, state = null) {
        const generation = state && state.generation;
        if (!watchTogetherStateIsCurrent(state, generation)) return null;
        const result = await watchTogetherRequest(WATCH_TOGETHER_ENDPOINTS.auth, {
            server_url: context.serverUrl,
            user_id: context.userId,
            api_key: context.accessToken,
        });
        if (!watchTogetherStateIsCurrent(state, generation)) return null;
        if (!result || typeof result.token !== 'string' || !result.token) {
            throw makeWatchTogetherError(503, 'invalid_auth_response', '本地服务返回的认证结果无效');
        }
        watchTogetherToken = result.token;
        watchTogetherTokenGeneration = generation;
        return result;
    }

    async function watchTogetherApiRequest(path, body, context, state = null, retried = false) {
        const generation = state && state.generation;
        if (!watchTogetherStateIsCurrent(state, generation)) return null;
        if (!watchTogetherToken || watchTogetherTokenGeneration !== generation) {
            await watchTogetherAuthenticate(context, state);
            if (!watchTogetherStateIsCurrent(state, generation)) return null;
        }
        try {
            const response = await watchTogetherRequest(path, body, watchTogetherToken);
            if (!watchTogetherStateIsCurrent(state, generation)) return null;
            return response;
        } catch (error) {
            if (!watchTogetherStateIsCurrent(state, generation)) return null;
            if (Number(error && error.status) === 401 && !retried) {
                watchTogetherInvalidateToken(state);
                await watchTogetherAuthenticate(context, state);
                if (!watchTogetherStateIsCurrent(state, generation)) return null;
                try {
                    const retryResponse = await watchTogetherRequest(path, body, watchTogetherToken);
                    if (!watchTogetherStateIsCurrent(state, generation)) return null;
                    return retryResponse;
                } catch (retryError) {
                    if (Number(retryError && retryError.status) === 401) watchTogetherInvalidateToken(state);
                    throw retryError;
                }
            }
            if (Number(error && error.status) === 401) watchTogetherInvalidateToken(state);
            throw error;
        }
    }

    function watchTogetherErrorMessage(error) {
        const status = Number(error && error.status) || 0;
        const code = String(error && error.code || '');
        if (code === 'timeout') return '连接本地服务超时，请确认 etlp 正在运行后重试。';
        if (code === 'network_error' || code === 'aborted' || status === 0) return '本地服务未运行或无法连接，请启动/重启 etlp 后重试。';
        if (status === 401) return '登录令牌已失效，请刷新 Emby 页面后重试。';
        if (code === 'server_mismatch') return '当前 Emby 实际服务器与 INI 中的 server_url 不一致，请改为同一服务器根 URL 后重启 etlp。';
        if (code === 'administrator_required') return '需要在 Emby 管理员账号页面操作，并在管理员本机配置 admin_enable。';
        if (status === 403) return '本地服务拒绝了该操作，请确认当前 Emby 账号权限和管理员配置。';
        if (status === 409) return '房间状态冲突或房间文件无效，请刷新列表后重试。';
        if (status === 503) return '同步观看服务暂不可用，请确认 enable/admin_enable 和服务器配置后重启 etlp。';
        return String(error && error.message || '同步观看请求失败，请稍后重试。');
    }

    function watchTogetherElement(tag, text = null, className = '') {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== null && text !== undefined) element.textContent = String(text);
        return element;
    }

    function watchTogetherInstallStyles() {
        if (document.getElementById('etlp-watch-together-style')) return;
        const style = document.createElement('style');
        style.id = 'etlp-watch-together-style';
        style.textContent = `
            #${WATCH_TOGETHER_PAGE_ID} { box-sizing:border-box; min-height:100%; padding:1.25em; color:currentColor; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-content { width:min(960px, 100%); margin:0 auto; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-header { display:flex; align-items:center; gap:.75em; margin-bottom:1.25em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-title { margin:0; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-status { margin-bottom:1em; padding:.75em 1em; border:1px solid currentColor; border-radius:.35em; white-space:pre-wrap; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1em; padding:1em; border:1px solid currentColor; border-radius:.35em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-field { display:grid; gap:.35em; min-width:0; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-field-wide { grid-column:1 / -1; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-form input, #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-form select { box-sizing:border-box; width:100%; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-actions { display:flex; flex-wrap:wrap; gap:.5em; margin-top:.75em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-rooms { display:grid; gap:.75em; margin-top:1.25em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-room { padding:1em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-room h3 { margin:0 0 .5em; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-room p { margin:.25em 0; }
            #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-empty { margin:1em 0 0; }
            #${WATCH_TOGETHER_PAGE_ID} button:disabled, #${WATCH_TOGETHER_PAGE_ID} select:disabled, #${WATCH_TOGETHER_PAGE_ID} input:disabled { cursor:wait; opacity:.55; }
            @media (max-width: 600px) {
                #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-form { grid-template-columns:1fr; }
                #${WATCH_TOGETHER_PAGE_ID} .etlp-wt-field-wide { grid-column:auto; }
            }
        `;
        (document.head || document.documentElement).appendChild(style);
    }

    function watchTogetherIsVisible(element) {
        if (!element || !(element instanceof Element)) return false;
        const style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
    }

    function watchTogetherPlatformName() {
        const client = getWatchTogetherApiClient();
        let name = '';
        try {
            name = String(client && (client._appName || client.appName) || serverName || '').toLowerCase();
        } catch (_) {
            name = String(serverName || '').toLowerCase();
        }
        if (name.includes('jellyfin')) return 'jellyfin';
        if (name.includes('plex')) return 'plex';
        if (name.includes('emby')) return 'emby';
        return null;
    }

    function watchTogetherFindNavContainer() {
        const containers = Array.from(document.querySelectorAll('.navDrawerItemsContainer.collapseContent'))
            .filter(watchTogetherIsVisible);
        if (!containers.length) return null;
        const metadataContainer = containers.find(container => /元数据管理器|metadata manager|metadata/i.test(String(container.textContent || '')));
        return metadataContainer || containers[0];
    }

    function watchTogetherCloseMobileDrawer() {
        const drawer = Array.from(document.querySelectorAll('.navDrawer, .navDrawerContainer'))
            .find(element => watchTogetherIsVisible(element) && (/open|visible/i.test(element.className) || element.getAttribute('aria-hidden') === 'false'));
        if (!drawer) return;
        const toggle = Array.from(document.querySelectorAll('.navDrawerButton, .btnNavDrawer, [data-action="togglemenu"]'))
            .find(watchTogetherIsVisible);
        if (toggle && typeof toggle.click === 'function') toggle.click();
    }

    function watchTogetherSetNavSelected(selected) {
        document.querySelectorAll(`[${WATCH_TOGETHER_NAV_DATA}]`).forEach(nav => {
            nav.classList.toggle('navMenuOption-selected', Boolean(selected && nav.id === WATCH_TOGETHER_NAV_ID));
        });
    }

    function watchTogetherEnsureNavigation() {
        if (watchTogetherPlatformName() !== 'emby') {
            document.querySelectorAll(`[${WATCH_TOGETHER_NAV_DATA}]`).forEach(nav => nav.remove());
            return;
        }
        const container = watchTogetherFindNavContainer();
        if (!container) {
            document.querySelectorAll(`[${WATCH_TOGETHER_NAV_DATA}]`).forEach(nav => nav.remove());
            return;
        }
        let nav = document.getElementById(WATCH_TOGETHER_NAV_ID);
        if (nav && nav.parentElement !== container) {
            nav.remove();
            nav = null;
        }
        document.querySelectorAll(`[${WATCH_TOGETHER_NAV_DATA}]`).forEach(candidate => {
            if (candidate !== nav) candidate.remove();
        });
        if (!nav) {
            nav = watchTogetherElement('button', null, 'listItem listItem-autoactive itemAction listItemCursor listItem-hoverable navMenuOption navDrawerListItem');
            nav.id = WATCH_TOGETHER_NAV_ID;
            nav.type = 'button';
            nav.setAttribute(WATCH_TOGETHER_NAV_DATA, 'true');
            nav.setAttribute('aria-label', '同步观看');
            const imageContainer = watchTogetherElement('div', null, 'navDrawerListItemImageContainer listItemImageContainer');
            const icon = watchTogetherElement('span', 'group', 'navDrawerListItemIcon listItemIcon md-icon');
            const body = watchTogetherElement('div', '同步观看', 'navDrawerListItemBody listItemBody');
            imageContainer.appendChild(icon);
            nav.append(imageContainer, body);
            nav.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
                watchTogetherCloseMobileDrawer();
                openWatchTogetherMenu().catch(() => alert('同步观看房间打开失败，请刷新 Emby 页面后重试。'));
            });
            container.appendChild(nav);
        }
        watchTogetherSetNavSelected(Boolean(watchTogetherActiveState && !watchTogetherActiveState.closed));
    }

    function watchTogetherFindMainPages(main) {
        if (!main) return [];
        return Array.from(main.children).filter(page => page.id !== WATCH_TOGETHER_PAGE_ID && watchTogetherIsVisible(page));
    }

    function watchTogetherRestoreCapturedPages(state) {
        if (!state || !Array.isArray(state.previousPages)) return;
        state.previousPages.forEach(previous => {
            if (!previous.element || !previous.element.isConnected) return;
            previous.element.style.display = previous.display;
            if (previous.hidden) previous.element.setAttribute('hidden', '');
            else previous.element.removeAttribute('hidden');
            if (previous.ariaHidden === null) previous.element.removeAttribute('aria-hidden');
            else previous.element.setAttribute('aria-hidden', previous.ariaHidden);
        });
    }

    function watchTogetherCleanupState(state, restore = false) {
        if (!state || state.closed) return;
        state.closed = true;
        if (watchTogetherActiveState === state) watchTogetherActiveState = null;
        if (state.onKeydown) document.removeEventListener('keydown', state.onKeydown, true);
        if (state.onNativeNavigation) document.removeEventListener('click', state.onNativeNavigation, true);
        if (state.onHashChange) window.removeEventListener('hashchange', state.onHashChange);
        if (state.onPopState) window.removeEventListener('popstate', state.onPopState);
        if (restore) watchTogetherRestoreCapturedPages(state);
        if (state.page && state.page.parentNode) state.page.remove();
        watchTogetherInvalidateToken(state);
        watchTogetherSetNavSelected(Boolean(watchTogetherActiveState && !watchTogetherActiveState.closed));
        watchTogetherScheduleNavigationSync();
    }

    function watchTogetherCreatePage() {
        if (watchTogetherActiveState) watchTogetherCleanupState(watchTogetherActiveState, false);
        const main = document.querySelector('.mainAnimatedPages.skinBody');
        if (!main) return null;
        watchTogetherInstallStyles();
        const previousPages = watchTogetherFindMainPages(main).map(element => ({
            element,
            display: element.style.display,
            hidden: element.hasAttribute('hidden'),
            ariaHidden: element.getAttribute('aria-hidden'),
        }));
        previousPages.forEach(previous => {
            previous.element.style.display = 'none';
            previous.element.setAttribute('aria-hidden', 'true');
        });

        const page = watchTogetherElement('div', null, 'view page focuscontainer-x mainAnimatedPage');
        page.id = WATCH_TOGETHER_PAGE_ID;
        page.setAttribute('data-etlp-watch-together-page', 'true');
        page.setAttribute('tabindex', '-1');
        const state = {
            generation: ++watchTogetherSessionGeneration,
            main,
            page,
            previousPages,
            users: [],
            runtime: [],
            closed: false,
        };
        const content = watchTogetherElement('div', null, 'etlp-wt-content');
        const header = watchTogetherElement('div', null, 'etlp-wt-header');
        const backButton = watchTogetherElement('button', '返回', 'emby-button button-link');
        backButton.type = 'button';
        backButton.setAttribute('aria-label', '返回 Emby 页面');
        const heading = watchTogetherElement('h1', '同步观看', 'etlp-wt-title');
        header.append(backButton, heading);
        const status = watchTogetherElement('div', '正在连接本地服务…', 'etlp-wt-status');
        status.setAttribute('role', 'status');
        const form = watchTogetherElement('form', null, 'etlp-wt-form');
        const nameLabel = watchTogetherElement('label', '房间名称', 'etlp-wt-field etlp-wt-field-wide');
        const nameInput = watchTogetherElement('input');
        nameInput.className = 'emby-input';
        nameInput.type = 'text';
        nameInput.maxLength = 120;
        nameInput.required = true;
        nameLabel.appendChild(nameInput);
        const userALabel = watchTogetherElement('label', '用户 A', 'etlp-wt-field');
        const userA = watchTogetherElement('select');
        userA.className = 'emby-select';
        userALabel.appendChild(userA);
        const userBLabel = watchTogetherElement('label', '用户 B', 'etlp-wt-field');
        const userB = watchTogetherElement('select');
        userB.className = 'emby-select';
        userBLabel.appendChild(userB);
        const primaryLabel = watchTogetherElement('label', '主用户（初始位置/冲突优先）', 'etlp-wt-field etlp-wt-field-wide');
        const primary = watchTogetherElement('select');
        primary.className = 'emby-select';
        primaryLabel.appendChild(primary);
        const createButton = watchTogetherElement('button', '创建房间', 'emby-button raised button-submit etlp-wt-field-wide');
        createButton.type = 'submit';
        form.append(nameLabel, userALabel, userBLabel, primaryLabel, createButton);
        const roomsHeading = watchTogetherElement('h2', '已有房间');
        const rooms = watchTogetherElement('div', null, 'etlp-wt-rooms');
        content.append(header, status, form, roomsHeading, rooms);
        page.appendChild(content);
        state.status = status;
        state.form = form;
        state.nameInput = nameInput;
        state.userA = userA;
        state.userB = userB;
        state.primary = primary;
        state.createButton = createButton;
        state.rooms = rooms;
        state.close = restore => watchTogetherCleanupState(state, restore);
        state.onKeydown = event => {
            if (event.key === 'Escape') state.close(true);
        };
        state.onNativeNavigation = event => {
            const target = event.target;
            const nativeNav = target && typeof target.closest === 'function' ? target.closest('.navMenuOption') : null;
            if (nativeNav && nativeNav.id !== WATCH_TOGETHER_NAV_ID) state.close(false);
        };
        state.onHashChange = () => state.close(false);
        state.onPopState = () => state.close(false);
        backButton.addEventListener('click', () => state.close(true));
        document.addEventListener('keydown', state.onKeydown, true);
        document.addEventListener('click', state.onNativeNavigation, true);
        window.addEventListener('hashchange', state.onHashChange);
        window.addEventListener('popstate', state.onPopState);
        watchTogetherActiveState = state;
        main.appendChild(page);
        watchTogetherBindUserSelects(state);
        watchTogetherBindCreate(state);
        watchTogetherSetNavSelected(true);
        return state;
    }

    function watchTogetherBindUserSelects(state) {
        if (!state || state.userListenersBound) return;
        state.userA.addEventListener('change', () => watchTogetherSyncPrimary(state));
        state.userB.addEventListener('change', () => watchTogetherSyncPrimary(state));
        state.userListenersBound = true;
    }

    function watchTogetherSetStatus(state, message) {
        if (watchTogetherStateIsCurrent(state) && state.status) state.status.textContent = String(message || '');
    }

    function watchTogetherSetFormDisabled(state, disabled) {
        if (!watchTogetherStateIsCurrent(state)) return;
        if (disabled) {
            [state.nameInput, state.userA, state.userB, state.primary, state.createButton].forEach(element => {
                if (element) element.disabled = true;
            });
            return;
        }
        state.nameInput.disabled = false;
        const unavailable = state.users.length < 2;
        state.userA.disabled = unavailable;
        state.userB.disabled = unavailable;
        state.primary.disabled = unavailable;
        state.createButton.disabled = unavailable;
    }

    function watchTogetherSyncPrimary(state) {
        if (!watchTogetherStateIsCurrent(state)) return;
        const previous = state.primary.value;
        state.primary.textContent = '';
        [state.userA.value, state.userB.value].filter((value, index, values) => value && values.indexOf(value) === index).forEach(userId => {
            const option = watchTogetherElement('option');
            option.value = userId;
            option.textContent = state.users.find(user => user.id === userId)?.name || userId;
            state.primary.appendChild(option);
        });
        if (Array.from(state.primary.options).some(option => option.value === previous)) state.primary.value = previous;
        else if (state.primary.options.length) state.primary.selectedIndex = 0;
    }

    function watchTogetherRenderUsers(state, users) {
        if (!watchTogetherStateIsCurrent(state)) return;
        state.users = Array.isArray(users) ? users.filter(user => user && user.id).map(user => ({ id: String(user.id), name: String(user.name || user.id) })) : [];
        [state.userA, state.userB].forEach(select => {
            select.textContent = '';
            state.users.forEach(user => {
                const option = watchTogetherElement('option');
                option.value = user.id;
                option.textContent = user.name;
                select.appendChild(option);
            });
        });
        if (state.userB.options.length > 1) state.userB.selectedIndex = 1;
        watchTogetherSyncPrimary(state);
        const unavailable = state.users.length < 2;
        state.userA.disabled = unavailable;
        state.userB.disabled = unavailable;
        state.primary.disabled = unavailable;
        state.createButton.disabled = unavailable;
    }

    function watchTogetherStatusLabel(state, runtimeError) {
        const labels = {
            waiting: '等待两人连接',
            barrier: '同步准备中',
            watching: '同步观看中',
            unavailable: '服务不可用',
            error: '发生错误',
        };
        const value = String(state || 'waiting').toLowerCase();
        const label = labels[value] || '状态未知';
        return runtimeError ? `${label}：${runtimeError}` : label;
    }

    function watchTogetherRenderRooms(state, roomList, runtimeList) {
        if (!watchTogetherStateIsCurrent(state)) return;
        state.runtime = Array.isArray(runtimeList) ? runtimeList : [];
        const runtimeByRoom = new Map(state.runtime.map(item => [String(item.room_id), item]));
        const usersById = new Map(state.users.map(user => [user.id, user.name]));
        state.rooms.textContent = '';
        if (!Array.isArray(roomList) || roomList.length === 0) {
            state.rooms.appendChild(watchTogetherElement('p', '暂无房间，请先创建一个房间。', 'etlp-wt-empty'));
            return;
        }
        roomList.forEach(room => {
            if (!room || !room.id) return;
            const roomId = String(room.id);
            const card = watchTogetherElement('article', null, 'etlp-wt-room');
            const title = watchTogetherElement('h3', String(room.name || '未命名房间'));
            const participantNames = Array.isArray(room.participant_user_ids)
                ? room.participant_user_ids.map(userId => usersById.get(String(userId)) || String(userId))
                : [];
            const participants = watchTogetherElement('p', `参与者：${participantNames.join('、') || '未知'}`);
            const primaryName = usersById.get(String(room.primary_user_id)) || String(room.primary_user_id || '未知');
            const primary = watchTogetherElement('p', `主用户：${primaryName}`);
            const runtime = runtimeByRoom.get(roomId) || {};
            const status = watchTogetherElement('p', `运行状态：${watchTogetherStatusLabel(runtime.state, runtime.error)}`);
            const actions = watchTogetherElement('div', null, 'etlp-wt-actions');
            [['pause', '暂停'], ['resume', '继续'], ['resync', '重新同步']].forEach(([action, text]) => {
                const button = watchTogetherElement('button', text, 'emby-button raised etlp-wt-secondary');
                button.type = 'button';
                button.addEventListener('click', () => watchTogetherRoomAction(state, roomId, action, button));
                actions.appendChild(button);
            });
            const deleteButton = watchTogetherElement('button', '删除', 'emby-button button-link etlp-wt-secondary');
            deleteButton.type = 'button';
            deleteButton.addEventListener('click', () => watchTogetherDeleteRoom(state, roomId, deleteButton));
            actions.appendChild(deleteButton);
            card.append(title, participants, primary, status, actions);
            state.rooms.appendChild(card);
        });
    }

    async function watchTogetherRefreshRooms(state, context, message = '正在加载房间…') {
        if (!watchTogetherStateIsCurrent(state)) return;
        watchTogetherSetStatus(state, message);
        try {
            const result = await watchTogetherApiRequest(WATCH_TOGETHER_ENDPOINTS.list, {}, context, state);
            if (!watchTogetherStateIsCurrent(state)) return;
            watchTogetherRenderUsers(state, result && result.users);
            watchTogetherRenderRooms(state, result && result.rooms, result && result.runtime);
            if (!result || !Array.isArray(result.rooms) || result.rooms.length === 0) watchTogetherSetStatus(state, '暂无房间，请先创建一个房间。');
            else watchTogetherSetStatus(state, '房间列表已更新。');
        } catch (error) {
            if (watchTogetherStateIsCurrent(state)) watchTogetherSetStatus(state, watchTogetherErrorMessage(error));
        }
    }

    async function watchTogetherRoomAction(state, roomId, action, button) {
        if (!watchTogetherStateIsCurrent(state) || !button) return;
        const context = state.context;
        button.disabled = true;
        try {
            await watchTogetherApiRequest(WATCH_TOGETHER_ENDPOINTS.action, { room_id: roomId, action }, context, state);
            if (!watchTogetherStateIsCurrent(state)) return;
            await watchTogetherRefreshRooms(state, context, '正在刷新房间状态…');
        } catch (error) {
            watchTogetherSetStatus(state, watchTogetherErrorMessage(error));
        } finally {
            if (watchTogetherStateIsCurrent(state)) button.disabled = false;
        }
    }

    async function watchTogetherDeleteRoom(state, roomId, button) {
        if (!watchTogetherStateIsCurrent(state) || !button) return;
        if (!window.confirm('确定删除此同步观看房间吗？')) return;
        const context = state.context;
        button.disabled = true;
        try {
            await watchTogetherApiRequest(WATCH_TOGETHER_ENDPOINTS.delete, { room_id: roomId }, context, state);
            if (!watchTogetherStateIsCurrent(state)) return;
            await watchTogetherRefreshRooms(state, context, '正在刷新房间列表…');
        } catch (error) {
            watchTogetherSetStatus(state, watchTogetherErrorMessage(error));
        } finally {
            if (watchTogetherStateIsCurrent(state)) button.disabled = false;
        }
    }

    function watchTogetherBindCreate(state) {
        state.form.addEventListener('submit', async event => {
            event.preventDefault();
            if (!watchTogetherStateIsCurrent(state)) return;
            const name = String(state.nameInput.value || '').trim();
            const members = [String(state.userA.value || ''), String(state.userB.value || '')];
            const primary = String(state.primary.value || '');
            if (!name) {
                watchTogetherSetStatus(state, '请填写房间名称。');
                return;
            }
            if (!members[0] || !members[1] || members[0] === members[1]) {
                watchTogetherSetStatus(state, '请选择两个不同的参与者。');
                return;
            }
            if (!primary || !members.includes(primary)) {
                watchTogetherSetStatus(state, '主用户必须是已选的参与者。');
                return;
            }
            watchTogetherSetFormDisabled(state, true);
            try {
                await watchTogetherApiRequest(WATCH_TOGETHER_ENDPOINTS.create, {
                    name,
                    participant_user_ids: members,
                    primary_user_id: primary,
                }, state.context, state);
                if (!watchTogetherStateIsCurrent(state)) return;
                state.nameInput.value = '';
                await watchTogetherRefreshRooms(state, state.context, '正在刷新房间列表…');
            } catch (error) {
                watchTogetherSetStatus(state, watchTogetherErrorMessage(error));
            } finally {
                if (watchTogetherStateIsCurrent(state)) {
                    watchTogetherSetFormDisabled(state, false);
                }
            }
        });
    }

    async function openWatchTogetherMenu() {
        const context = getWatchTogetherApiContext();
        if (context.error) {
            alert(context.error);
            return;
        }
        const state = watchTogetherCreatePage();
        if (!state) {
            alert('同步观看房间需要在 Emby 页面打开，当前页面尚未准备好。');
            return;
        }
        state.context = context;
        watchTogetherSetStatus(state, '正在验证 Emby 账号…');
        try {
            await watchTogetherAuthenticate(context, state);
            if (!watchTogetherStateIsCurrent(state)) return;
            await watchTogetherRefreshRooms(state, context);
        } catch (error) {
            if (watchTogetherStateIsCurrent(state)) watchTogetherSetStatus(state, watchTogetherErrorMessage(error));
        }
    }

    function watchTogetherScheduleNavigationSync() {
        if (watchTogetherNavigationFrame) return;
        const schedule = () => {
            watchTogetherNavigationFrame = 0;
            watchTogetherEnsureNavigation();
            const state = watchTogetherActiveState;
            if (!state || state.closed) return;
            if (!state.page.isConnected || watchTogetherFindMainPages(state.main).length) state.close(false);
        };
        if (typeof window.requestAnimationFrame === 'function') watchTogetherNavigationFrame = window.requestAnimationFrame(schedule);
        else watchTogetherNavigationFrame = window.setTimeout(schedule, 0);
    }

    function watchTogetherStartNavigationObserver() {
        if (watchTogetherNavigationStarted) return;
        watchTogetherNavigationStarted = true;
        const start = () => {
            watchTogetherScheduleNavigationSync();
            if (!document.documentElement || watchTogetherNavigationObserver) return;
            watchTogetherNavigationObserver = new MutationObserver(() => watchTogetherScheduleNavigationSync());
            watchTogetherNavigationObserver.observe(document.documentElement, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'],
            });
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
        start();
    }

    // --- end watch-together administrator UI ---

    function hideCurrentSeries() {
        const urlMatch = window.location.href.match(/id=(\d+)/);
        let hint = '请在需要隐藏的电视剧【条目根页面】操作';
        if (!urlMatch) {
            alert(hint);
            return;
        }

        const seriesId = urlMatch[1];
        if (!seriesId) {
            alert(hint);
            return;
        }

        let hideList = [];
        const stored = localStorage.getItem(etlpStorageKeys.hideSeriesIds);
        if (stored) {
            try {
                hideList = JSON.parse(stored);
            } catch (e) {
                logger.error('解析隐藏列表失败:', e);
                hideList = [];
            }
        }

        if (!hideList.includes(seriesId)) {
            hideList.push(seriesId);
            localStorage.setItem(etlpStorageKeys.hideSeriesIds, JSON.stringify(hideList));
            logger.info('已隐藏电视剧, SeriesId:', seriesId);
            alert(`已隐藏该电视剧，注意要电视剧条目主页面操作 SeriesId=${seriesId}`);
        } else {
            alert('该电视剧已在隐藏列表中');
        }
    }

    function resetHiddenSeries() {
        localStorage.removeItem(etlpStorageKeys.hideSeriesIds);
        logger.info('已重置隐藏设置');
        alert('已重置隐藏设置,刷新页面后生效');
    }

    function removeErrorWindows() {
        let okButtonList = document.querySelectorAll('button[data-id="ok"]');
        let state = false;
        for (let index = 0; index < okButtonList.length; index++) {
            const element = okButtonList[index];
            if (element.textContent.search(/.+/) != -1) {
                element.click();
                if (isHidden(element)) { continue; }
                state = true;
            }
        }

        let jellyfinSpinner = document.querySelector('div.docspinner');
        if (jellyfinSpinner) {
            jellyfinSpinner.remove();
            state = true;
        };

        let plexErrorSelector = '[class*="Modal-small"] [class*="ModalContent-modalContent"] [class*="PlayerErrorModal-modalHeader"]';
        if (document.querySelector(plexErrorSelector)) {
            let escEvent = new KeyboardEvent('keydown', {
                key: 'Escape',
                keyCode: 27,
                code: 'Escape',
                which: 27,
                bubbles: true,
            });
            document.dispatchEvent(escEvent);
            state = true;
        }

        return state;
    }

    async function removeErrorWindowsMultiTimes() {
        for (const times of Array(15).keys()) {
            await sleep(200);
            if (removeErrorWindows()) {
                logger.info(`remove error window used time: ${(times + 1) * 0.2}`);
                break;
            };
        }
    }

    function sendDataToLocalServer(data, path) {
        let url = `http://127.0.0.1:58000/${path}/`;
        GM_xmlhttpRequest({
            method: 'POST',
            url: url,
            data: JSON.stringify(data),
            headers: {
                'Content-Type': 'application/json'
            },
            onerror: function (error) {
                alert(`${url}\n请求错误，本地服务未运行，请查看使用说明。\nhttps://github.com/kjtsune/embyToLocalPlayer`);
                console.error('请求错误:', error);
            }
        });
        logger.info(path, data);
    }

    let serverName = null;
    let episodesInfoCache = []; // ['type:[Episodes|NextUp|Items]', resp]
    let episodesInfoRe = /\/Episodes\?IsVirtual|\/NextUp\?Series|\/Items\?ParentId=\w+&Filters=IsNotFolder&Recursive=true/; // Items已排除播放列表
    // 点击位置：Episodes 继续观看，如果是即将观看，可能只有一集的信息 | NextUp 新播放或媒体库播放 | Items 季播放。 只有 Episodes 返回所有集的数据。
    let playlistInfoCache = null;
    let resumeRawInfoCache = null;
    let resumePlaybackCache = {};
    let resumeItemDataCache = {};
    let allPlaybackCache = {};
    let allItemDataCache = {};
    let episodesWithPathCache = {};

    let metadataChangeRe = /\/MetadataEditor|\/Refresh\?/;
    let metadataMayChange = false;

    function cleanOptionalCache() {
        resumeRawInfoCache = null;
        resumePlaybackCache = {};
        resumeItemDataCache = {};
        allPlaybackCache = {};
        allItemDataCache = {};
        episodesInfoCache = [];
        episodesWithPathCache = {};
    }

    function throttle(fn, delay) {
        let lastTime = 0;
        return function (...args) {
            const now = Date.now();
            if (now - lastTime >= delay) {
                lastTime = now;
                fn.apply(this, args);
            }
        };
    }

    let addOpenFolderElement = throttle(_addOpenFolderElement, 100);

    async function _addOpenFolderElement(itemId) {
        if (config.disableOpenFolder) return;
        let mediaSources = null;
        for (const _ of Array(5).keys()) {
            await sleep(500);
            mediaSources = getVisibleElement(document.querySelectorAll('div.mediaSources'));
            if (mediaSources) break;
        }
        if (!mediaSources) return;
        let pathDiv = mediaSources.querySelector('div[class^="sectionTitle sectionTitle-cards"] > div');
        if (!pathDiv || pathDiv.className == 'mediaInfoItems' || pathDiv.id == 'addFileNameElement') return;
        let full_path = pathDiv.textContent;
        if (!full_path.match(/[\\/:]/)) return;
        if (full_path.match(/\d{1,3}\.?\d{0,2} (MB|GB)/)) return;

        let itemData = (itemId in allItemDataCache) ? allItemDataCache[itemId] : null
        let strmFile = (full_path.startsWith('http')) ? itemData?.Path : null

        let openButtonHtml = `<a id="openFolderButton" is="emby-linkbutton" class="raised item-tag-button 
        nobackdropfilter emby-button" ><i class="md-icon button-icon button-icon-left">link</i>Open Folder</a>`
        pathDiv.insertAdjacentHTML('beforebegin', openButtonHtml);
        let btn = mediaSources.querySelector('a#openFolderButton');
        if (strmFile) {
            pathDiv.innerHTML = pathDiv.innerHTML + '<br>' + strmFile;
            full_path = strmFile; // emby 会把 strm 内的链接当路径展示
        }
        btn.addEventListener('click', () => {
            logger.info(full_path);
            sendDataToLocalServer({ full_path: full_path }, 'openFolder');
        });
    }

    async function addFileNameElement(resp) {
        let mediaSources = null;
        for (const _ of Array(5).keys()) {
            await sleep(500);
            mediaSources = getVisibleElement(document.querySelectorAll('div.mediaSources'));
            if (mediaSources) break;
        }
        if (!mediaSources) return;
        let pathDivs = mediaSources.querySelectorAll('div[class^="sectionTitle sectionTitle-cards"] > div');
        if (!pathDivs) return;
        pathDivs = Array.from(pathDivs);
        let _pathDiv = pathDivs[0];
        if (_pathDiv.id == 'addFileNameElement') return;
        let isAdmin = !/\d{4}\/\d+\/\d+/.test(_pathDiv.textContent); // 非管理员只有包含添加日期的文件类型 div
        let isStrm = _pathDiv.textContent.startsWith('http');
        if (isAdmin) {
            if (!isStrm) { return; }
            pathDivs = pathDivs.filter((_, index) => index % 2 === 0); // 管理员一个文件同时有路径和文件类型两个 div
        }

        let sources = await resp.clone().json();
        sources = sources.MediaSources;
        for (let index = 0; index < pathDivs.length; index++) {
            const pathDiv = pathDivs[index];
            let fileName = sources[index].Name; // 多版本的话，是版本名。
            let filePath = sources[index].Path;
            let strmFile = filePath.startsWith('http');
            if (!strmFile) {
                fileName = filePath.split('\\').pop().split('/').pop();
                fileName = (config.crackFullPath && !isAdmin) ? filePath : fileName;
            }
            let fileDiv = `<div id="addFileNameElement">${fileName}</div> `
            if (strmFile && (!isAdmin && config.crackFullPath)) {
                fileDiv = `<div id="addFileNameElement">${fileName}<br>${filePath}</div> `
            }
            pathDiv.insertAdjacentHTML('beforebegin', fileDiv);
        }
    }

    function makeItemIdCorrect(itemId) {
        if (serverName !== 'emby') { return itemId; }
        if (!resumeRawInfoCache || !episodesInfoCache) { return itemId; }
        let resumeIds = resumeRawInfoCache.map(item => item.Id);
        if (resumeIds.includes(itemId)) { return itemId; }
        let pageId = window.location.href.match(/\/item\?id=(\d+)/)?.[1];
        if (resumeIds.includes(pageId) && itemId == episodesInfoCache[0].Id) {
            // 解决从继续观看进入集详情页时，并非播放第一集，却请求首集视频文件信息导致无法播放。
            // 手动解决方法：从下方集卡片点击播放，或从集卡片再次进入集详情页后播放。
            // 本函数的副作用：集详情页底部的第一集卡片点播放按钮会播放当前集。
            // 副作用解决办法：再点击一次，或者点第一集卡片进入详情页后再播放。不过一般也不怎么会回头看第一集。
            return pageId;

        } else if (window.location.href.match(/serverId=/)) {
            return itemId; // 仅处理首页继续观看和集详情页，其他页面忽略。
        }
        let correctSeaId = episodesInfoCache.find(item => item.Id == itemId)?.SeasonId;
        let correctItemId = resumeRawInfoCache.find(item => item.SeasonId == correctSeaId)?.Id;
        if (correctSeaId && correctItemId) {
            logger.info(`makeItemIdCorrect, old=${itemId}, new=${correctItemId}`)
            return correctItemId;
        }
        return itemId;
    }

    async function embyToLocalPlayer(playbackUrl, request, playbackData, extraData) {
        let data = {
            ApiClient: ApiClient,
            playbackData: playbackData,
            playbackUrl: playbackUrl,
            request: request,
            mountDiskEnable: localStorage.getItem(etlpStorageKeys.mountDiskEnable),
            extraData: extraData,
            fistTime: fistTime,
        };
        sendDataToLocalServer(data, 'embyToLocalPlayer');
        removeErrorWindowsMultiTimes();
        fistTime = false;
    }

    async function apiClientGetWithCache(itemId, cacheList, funName) {
        if (!itemId) {
            logger.info(`Skip ${funName} ${itemId}`);
        }
        for (const cache of cacheList) {
            if (itemId in cache) {
                logger.info(`HIT ${funName} itemId=${itemId}`)
                return cache[itemId];
            }
        }
        logger.info(`MISS ${funName} itemId=${itemId}`)
        let resInfo;
        switch (funName) {
            case 'getPlaybackInfo':
                resInfo = await ApiClient.getPlaybackInfo(itemId);
                break;
            case 'getItem':
                resInfo = await ApiClient.getItem(ApiClient._serverInfo.UserId, itemId);
                break;
            case 'getEpisodes':
                {
                    let seasonId = itemId;
                    let options = {
                        'Fields': 'MediaSources,Path,ProviderIds',
                        'SeasonId': seasonId,
                    }
                    resInfo = await ApiClient.getEpisodes(seasonId, options);
                    break;
                }
            default:
                break;
        }
        for (const cache of cacheList) {
            if (funName == 'getPlaybackInfo') {
                // strm ffprobe 处理前后的外挂字幕 index 会变化，故不缓存。
                let runtime = resInfo?.MediaSources?.[0]?.RunTimeTicks;
                if (!runtime)
                    break;
            }
            cache[itemId] = resInfo;
        }
        return resInfo;
    }

    async function getPlaybackWithCace(itemId) {
        return apiClientGetWithCache(itemId, [resumePlaybackCache, allPlaybackCache], 'getPlaybackInfo');
    }

    async function getItemInfoWithCace(itemId) {
        return apiClientGetWithCache(itemId, [resumeItemDataCache, allItemDataCache], 'getItem');
    }

    async function getEpisodesWithCace(seasonId) {
        return apiClientGetWithCache(seasonId, [episodesWithPathCache], 'getEpisodes');
    }

    async function dealWithPlaybackInfo(raw_url, url, options) {
        console.time('dealWithPlaybackInfo');
        let rawId = url.match(/\/Items\/(\w+)\/PlaybackInfo/)[1];
        episodesInfoCache = episodesInfoCache[0] ? episodesInfoCache[1].clone() : null;
        let itemId = rawId;
        let [playbackData, mainEpInfo, episodesInfoData] = await Promise.all([
            getPlaybackWithCace(itemId), // originFetch(raw_url, request), 可能会 NoCompatibleStream
            getItemInfoWithCace(itemId),
            episodesInfoCache?.json(),
        ]);
        console.timeEnd('dealWithPlaybackInfo');
        episodesInfoData = (episodesInfoData && episodesInfoData.Items) ? episodesInfoData.Items : null;
        episodesInfoCache = episodesInfoData;
        let correctId = makeItemIdCorrect(itemId);
        url = url.replace(`/${rawId}/`, `/${correctId}/`)
        if (itemId != correctId) {
            itemId = correctId;
            [playbackData, mainEpInfo] = await Promise.all([
                getPlaybackWithCace(itemId),
                getItemInfoWithCace(itemId),
            ]);
            let startPos = mainEpInfo.UserData.PlaybackPositionTicks;
            url = url.replace('StartTimeTicks=0', `StartTimeTicks=${startPos}`);
        }
        let playlistData = (playlistInfoCache && playlistInfoCache.Items) ? playlistInfoCache.Items : null;
        episodesInfoCache = []
        let extraData = {
            mainEpInfo: mainEpInfo,
            episodesInfo: episodesInfoData,
            playlistInfo: playlistData,
            gmInfo: GM_info,
            userAgent: navigator.userAgent,
        }
        playlistInfoCache = null;
        // resumeInfoCache = null;
        logger.info(extraData);
        if (mainEpInfo?.Type == 'Trailer') {
            alert('etlp: Does not support Trailers plugin. Please disable it.');
            return false;
        }
        if (config.disableForLiveTv && mainEpInfo?.Type == 'TvChannel') { return 'disableForLiveTv'; }
        let notBackdrop = Boolean(playbackData.MediaSources[0].Path.search(/\Wbackdrop/i) == -1);
        if (notBackdrop) {
            let _req = options ? options : raw_url;
            playNotifiy();
            embyToLocalPlayer(url, _req, playbackData, extraData);
            return true;
        }
        return false;
    }

    async function deailWithItemInfo(item) {
        let itemId = item.Id;
        let seasonId = item.SeasonId;

        let [mainEpInfo, playbackData, episodesInfoData] = await Promise.all([
            getItemInfoWithCace(itemId),
            getPlaybackWithCace(itemId),
            (seasonId) ? getEpisodesWithCace(seasonId) : null,
        ]);

        let positonTicks = item.UserData.PlaybackPositionTicks;
        let userId = ApiClient._serverInfo.UserId;
        let deviceId = ApiClient._deviceId;
        let accessToken = ApiClient._userAuthInfo?.AccessToken || ApiClient._serverInfo?.AccessToken;
        if (!accessToken) {
            playNotifiy('Not accessToken');
        }
        let urlParams = {
            'X-Emby-Device-Id': deviceId,
            'StartTimeTicks': positonTicks,
            'X-Emby-Token': accessToken,
            'UserId': userId,
            'IsPlayback': true
        };
        let baseUrl = `${window.location.origin}/emby/Items/${itemId}/PlaybackInfo`;
        let searchParams = new URLSearchParams(urlParams);
        let playbackUrl = `${baseUrl}?${searchParams.toString()}`;
        let episodesInfo = episodesInfoData?.Items || [];
        let extraData = {
            mainEpInfo: mainEpInfo,
            episodesInfo: episodesInfo,
            playlistInfo: [],
            gmInfo: GM_info,
            userAgent: navigator.userAgent,
        }
        embyToLocalPlayer(playbackUrl, {}, playbackData, extraData)
    }

    document.addEventListener('click', e => {
        if (localStorage.getItem(etlpStorageKeys.webPlayerEnable) == 'true') { return; }
        // if (window.location.hash != '#!/home') { return; }
        const cardPlayBtn = e.target.closest('button.cardOverlayFab-primary[data-action="play"]');
        // 最新电视和媒体库电视会是 "resume" 需要额外请求 nextup 获取季和集信息。但多版本会只返回一个版本。播放前又要请求多版本信息来确定。
        // const cardPlayBtn = e.target.closest('button.cardOverlayFab-primary[data-action="play"], button.cardOverlayFab-primary[data-action="resume"]');
        // const listPlayBtn = e.target.closest('button.listItem[data-id="resume"][data-action="custom"]');
        // const listShuffleBtn = e.target.closest('button.listItem[data-id="shuffle"][data-action="custom"]');
        const playButton = cardPlayBtn;

        if (!playButton) {
            return;
        }
        const container = e.target.closest('div[is="emby-itemscontainer"]');
        if (!container || (!container._itemSource && !container.items)) {
            logger.info('🎬 Play button clicked, but not within a recognized item list container.');
            return;
        }
        const parentCard = e.target.closest('.virtualScrollItem.card, .backdropCard[data-index]');
        if (!parentCard) {
            return;
        }

        const index = parentCard._dataItemIndex ?? parentCard.dataset.index;
        const itemList = container._itemSource || container.items;
        const item = itemList[index];
        const action = playButton.dataset.action || playButton.dataset.mode;
        let itemType = item.Type;
        if (!['Movie', 'Episode'].includes(itemType)) {
            logger.info('🎬 Play button clicked, but not within legal itemType.');
            return
        }
        logger.info(`🎬 Action '${action}' triggered for item at index ${index}:`, item);
        e.preventDefault();
        e.stopImmediatePropagation();
        deailWithItemInfo(item);
        let title = item.SeriesName || item.Name;
        let subTitle = item.SeriesName && item.Name || item.ProductionYear;
        playNotifiy(title, subTitle);
    }, true);

    async function cacheResumeItemInfo() {
        let inInit = !myBool(resumeRawInfoCache);
        let resumeIds;
        let storageKey = etlpStorageKeys.cacheResumeIds;
        if (inInit) {
            resumeIds = localStorage.getItem(storageKey)
            if (resumeIds) {
                resumeIds = JSON.parse(resumeIds);
            } else {
                return
            }
        } else {
            resumeIds = resumeRawInfoCache.slice(0, 5).map(item => item.Id);
            let seasonIds = resumeRawInfoCache.slice(0, 5).map(item => item.SeasonId);
            await Promise.all(seasonIds.filter(Boolean).map(sid => getEpisodesWithCace(sid)));
            localStorage.setItem(storageKey, JSON.stringify(resumeIds));
        }

        for (let [globalCache, getFun] of [[resumePlaybackCache, getPlaybackWithCace], [resumeItemDataCache, getItemInfoWithCace]]) {
            let cacheDataAcc = {};
            if (myBool(globalCache)) {
                cacheDataAcc = globalCache;
                resumeIds = resumeIds.filter(id => !(id in globalCache));
                if (resumeIds.length == 0) { return; }
            }
            let itemInfoList = await Promise.all(
                resumeIds.map(id => getFun(id))
            )
            globalCache = itemInfoList.reduce((acc, result, index) => {
                acc[resumeIds[index]] = result;
                return acc;
            }, cacheDataAcc);
        }

    }

    async function cloneAndCacheFetch(resp, key, cache) {
        try {
            const data = await resp.clone().json();
            cache[key] = data;
            return data;
        } catch (_error) {
            // pass
        }
    }

    let itemInfoRe = /\/Items\/(\w+)\?/; // 要严格些，不然手动标记已播放 PlayedItems 也会命中，造成缓存错误数据。

    unsafeWindow.fetch = async (input, options) => {
        let isStrInput = typeof input === 'string';
        let urlStr = isStrInput ? input : input.url;

        if (serverName === null) {
            serverName = typeof ApiClient === 'undefined' ? null : ApiClient._appName.split(' ')[0].toLowerCase();
        } else {
            if (typeof ApiClient != 'undefined' && ApiClient._deviceName != 'embyToLocalPlayer' && localStorage.getItem(etlpStorageKeys.webPlayerEnable) != 'true') {
                ApiClient._deviceName = 'embyToLocalPlayer'
                cacheResumeItemInfo();
            }
        }
        if (metadataMayChange && urlStr.includes('Items')) {
            if (urlStr.includes('reqformat') && !urlStr.includes('fields')) {
                cleanOptionalCache();
                metadataMayChange = false;
                logger.info('cleanOptionalCache by metadataMayChange')
            }
        }
        // 适配播放列表及媒体库的全部播放、随机播放。会禁用版本筛选和美化标题。
        if (urlStr.includes('Items?') && /Limit=(300|1000|5\d\d\d)/.test(urlStr)) {
            let _resp = await originFetch(input, options);
            if (serverName == 'emby') {
                await ApiClient._userViewsPromise?.then(result => {
                    let viewsItems = result.Items;
                    let viewsIds = [];
                    viewsItems.forEach(item => {
                        viewsIds.push(item.Id);
                    });
                    let viewsRegex = viewsIds.join('|');
                    viewsRegex = `ParentId=(${viewsRegex})`
                    if (!RegExp(viewsRegex).test(urlStr)) { // 点击季播放美化标题所需，并非媒体库随机播放。
                        episodesInfoCache = ['Items', _resp.clone()]
                        logger.info('episodesInfoCache', episodesInfoCache);
                        logger.info('viewsRegex', viewsRegex);
                        return _resp;
                    }
                }).catch(error => {
                    console.error('Error occurred: ', error);
                });
            }

            playlistInfoCache = null;
            let _resd = await _resp.clone().json();
            if (!_resd.Items[0]) {
                logger.error('playlist is empty, skip');
                return _resp;
            }
            if (['Movie', 'MusicVideo', 'Episode'].includes(_resd.Items[0].Type)) {
                playlistInfoCache = _resd
                logger.info('playlistInfoCache', playlistInfoCache);
            }
            return _resp
        }
        // 获取各集标题等，仅用于美化标题，放后面避免误拦截首页右键媒体库随机播放数据。
        let _epMatch = urlStr.match(episodesInfoRe);
        if (_epMatch) {
            _epMatch = _epMatch[0].split(['?'])[0].substring(1); // Episodes|NextUp|Items
            let _resp = await originFetch(input, options);
            episodesInfoCache = [_epMatch, _resp.clone()]
            logger.info('episodesInfoCache', episodesInfoCache);
            return _resp
        }

        if (urlStr.includes('Items/Resume') && urlStr.includes('MediaTypes=Video')) {
            let reqUrl = urlStr;

            if (config.enableResumeReorder) {
                reqUrl = urlStr.replace(/Fields=([^&]*)/, 'Fields=$1,DateCreated');
            }

            let fetchInput = isStrInput ? reqUrl : new Request(reqUrl, input);

            let _resp = await originFetch(fetchInput, options);
            let _resd = await _resp.clone().json();

            // 处理隐藏特定电视剧
            if (config.resumeHideSomeSeries && _resd.Items && _resd.Items.length > 0) {
                const hideListStr = localStorage.getItem(etlpStorageKeys.hideSeriesIds);
                if (hideListStr) {
                    try {
                        const hideList = JSON.parse(hideListStr);
                        const originalLength = _resd.Items.length;
                        _resd.Items = _resd.Items.filter(item => {
                            if (!item.SeriesId) return true;
                            return !hideList.includes(item.SeriesId);
                        });
                        const hiddenCount = originalLength - _resd.Items.length;
                        if (hiddenCount > 0) {
                            logger.info(`已隐藏 ${hiddenCount} 个电视剧条目`);
                        }
                    } catch (e) {
                        logger.error('解析隐藏列表失败:', e);
                    }
                }
            }

            if (config.enableResumeReorder && _resd.Items && _resd.Items.length > 2) {
                const now = new Date();
                const threeDaysAgo = new Date(now.getTime() - 3 * 24 * 60 * 60 * 1000);
                const firstTwo = _resd.Items.slice(0, 2);
                const rest = _resd.Items.slice(2);
                const recentItems = [];
                const olderItems = [];
                rest.forEach(item => {
                    const dateCreated = new Date(item.DateCreated);
                    if (dateCreated >= threeDaysAgo) {
                        recentItems.push(item);
                    } else {
                        olderItems.push(item);
                    }
                });
                _resd.Items = [...firstTwo, ...recentItems, ...olderItems];
                logger.info(`重排序完成: 前2位保持, ${recentItems.length}个近3天项目前移, ${olderItems.length}个旧项目后移`);
            }

            const modifiedBody = JSON.stringify(_resd);
            const modifiedResponse = new Response(modifiedBody, {
                status: _resp.status,
                statusText: _resp.statusText,
                headers: _resp.headers
            });

            resumeRawInfoCache = _resd.Items;
            cacheResumeItemInfo();
            logger.info('resumeRawInfoCache', resumeRawInfoCache);

            return modifiedResponse;
        }
        // 缓存 itemInfo ，可能匹配到 Items/Resume，故放后面。
        if (urlStr.match(itemInfoRe)) {
            let itemId = urlStr.match(itemInfoRe)[1];
            let resp = await originFetch(input, options);
            logger.info(`CACHE allItemDataCache itemId=${itemId}`);
            cloneAndCacheFetch(resp, itemId, allItemDataCache);
            return resp;
        }
        try {
            if (urlStr.indexOf('/PlaybackInfo?UserId') != -1) {
                if (urlStr.indexOf('IsPlayback=true') != -1 && localStorage.getItem(etlpStorageKeys.webPlayerEnable) != 'true') {
                    let dealRes = await dealWithPlaybackInfo(input, urlStr, options);
                    if (dealRes && dealRes != 'disableForLiveTv') { return; }
                } else {
                    let itemId = urlStr.match(/\/Items\/(\w+)\/PlaybackInfo/)[1];
                    let resp = await originFetch(input, options);
                    addFileNameElement(resp.clone()); // itemId data 不包含多版本的文件信息，故用不到
                    addOpenFolderElement(itemId);
                    logger.info(`CACHE allPlaybackCache itemId=${itemId}`);
                    cloneAndCacheFetch(resp.clone(), itemId, allPlaybackCache);
                    return resp;
                }
            } else if (urlStr.indexOf('/Playing/Stopped') != -1 && localStorage.getItem(etlpStorageKeys.webPlayerEnable) != 'true') {
                return
            }
        } catch (error) {
            logger.error(error, input, urlStr);
            removeErrorWindowsMultiTimes();
            return
        }

        if (urlStr.match(metadataChangeRe)) {
            if (urlStr.includes('MetadataEditor')) {
                metadataMayChange = true;
            } else {
                cleanOptionalCache();
                logger.info('cleanOptionalCache by Refresh')
            }
        }
        return originFetch(input, options);
    }

    function initXMLHttpRequest() {

        const originOpen = XMLHttpRequest.prototype.open;
        const originSend = XMLHttpRequest.prototype.send;
        const originSetHeader = XMLHttpRequest.prototype.setRequestHeader;

        XMLHttpRequest.prototype.setRequestHeader = function (header, value) {
            this._headers[header] = value;
            return originSetHeader.apply(this, arguments);
        }

        XMLHttpRequest.prototype.open = function (method, url) {
            this._method = method;
            this._url = url;
            this._headers = {};

            if (serverName === null && this._url.indexOf('X-Plex-Product') != -1) { serverName = 'plex' };
            let catchPlex = (serverName == 'plex' && this._url.indexOf('playQueues?type=video') != -1)
            if (catchPlex && localStorage.getItem(etlpStorageKeys.webPlayerEnable) != 'true') { // Plex
                fetch(this._url, {
                    method: this._method,
                    headers: {
                        'Accept': 'application/json',
                    }
                })
                    .then(response => response.json())
                    .then((res) => {
                        let extraData = {
                            gmInfo: GM_info,
                            userAgent: navigator.userAgent,
                        };
                        let data = {
                            playbackData: res,
                            playbackUrl: this._url,
                            mountDiskEnable: localStorage.getItem(etlpStorageKeys.mountDiskEnable),
                            extraData: extraData,
                        };
                        sendDataToLocalServer(data, 'plexToLocalPlayer');
                        removeErrorWindowsMultiTimes();
                    });
                return;
            }
            return originOpen.apply(this, arguments);
        }

        XMLHttpRequest.prototype.send = function (body) {

            let catchJellyfin = (this._method === 'POST' && this._url.endsWith('PlaybackInfo'))
            if (catchJellyfin && localStorage.getItem(etlpStorageKeys.webPlayerEnable) != 'true') { // Jellyfin 10.10
                let pbUrl = this._url;
                body = JSON.parse(body);
                let _body = {};
                ['MediaSourceId', 'StartTimeTicks', 'UserId', 'SubtitleStreamIndex', 'AudioStreamIndex',].forEach(key => {
                    if (body[key] != undefined) {
                        _body[key] = body[key];
                    }
                });
                let query = new URLSearchParams(_body).toString();
                pbUrl = `${pbUrl}?${query}`
                let options = {
                    headers: this._headers,
                };
                dealWithPlaybackInfo(pbUrl, pbUrl, options);
                return;
            }
            originSend.apply(this, arguments);
        }
    }

    initXMLHttpRequest();
    watchTogetherStartNavigationObserver();

    setModeSwitchMenu(etlpStorageKeys.webPlayerEnable, '脚本在当前服务器 已', '', '可用', '禁用', '可用');
    setModeSwitchMenu(etlpStorageKeys.mountDiskEnable, '读取硬盘模式已经 ');
    setCallbackMenu('同步观看房间', () => {
        openWatchTogetherMenu().catch(() => {
            alert('同步观看房间打开失败，请刷新 Emby 页面后重试。');
        });
    });

    function showGuiMenu() {
        sendDataToLocalServer({ 'showTaskManager': true }, 'embyToLocalPlayer');
    }
    if ('etlpTaskManager' in localStorage) {
        setCallbackMenu('查看缓存任务', showGuiMenu);
    }

    overwriteConfByStore();

    if (config.resumeHideSomeSeries || localStorage.getItem(etlpStorageKeys.resumeHide) === 'true') {
        setCallbackMenu('继续播放: 隐藏该电视剧', hideCurrentSeries);
        setCallbackMenu('继续播放: 重置隐藏设置', resetHiddenSeries);
    }

})();
