const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(ROOT, 'user_script', 'embyToLocalPlayer.user.js');
const SOURCE = fs.readFileSync(SCRIPT, 'utf8');

function extract(startMarker, endMarker) {
    const start = SOURCE.indexOf(startMarker);
    const end = SOURCE.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(start, -1, `missing start marker: ${startMarker}`);
    assert.notEqual(end, -1, `missing end marker: ${endMarker}`);
    return SOURCE.slice(start, end);
}

function loadLogger() {
    const calls = [];
    const snippet = extract('    const originFetch = fetch;', '    function myBool');
    const sandbox = {
        URL,
        URLSearchParams,
        WeakSet,
        console: { log: (...args) => calls.push(args) },
        fetch: () => Promise.resolve(),
    };
    const logger = vm.runInNewContext(`(() => {
        const config = { logLevel: 3 };
        ${snippet}
        return logger;
    })()`, sandbox);
    return { logger, calls };
}

function stringifyCalls(calls) {
    return JSON.stringify(calls, (_key, value) => {
        if (typeof value === 'bigint') return String(value);
        return value;
    });
}

class FakeText {
    constructor(text) {
        this.nodeType = 3;
        this.nodeValue = String(text);
        this.parentNode = null;
    }

    get textContent() {
        return this.nodeValue;
    }

    set textContent(value) {
        this.nodeValue = String(value ?? '');
    }
}

class FakeElement {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.style = { cssText: '', animation: '' };
        this.id = '';
        this.className = '';
        this._text = undefined;
        this.innerHTML = '';
        this.listeners = {};
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    insertBefore(child, reference) {
        child.parentNode = this;
        const index = this.children.indexOf(reference);
        if (index < 0) this.children.push(child);
        else this.children.splice(index, 0, child);
        return child;
    }

    addEventListener(type, callback) {
        this.listeners[type] = this.listeners[type] || [];
        this.listeners[type].push(callback);
    }

    remove() {
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter(child => child !== this);
        this.parentNode = null;
    }

    get textContent() {
        if (this._text !== undefined) return this._text;
        return this.children.map(child => child.textContent).join('');
    }

    set textContent(value) {
        this._text = undefined;
        this.children = [];
        const text = String(value ?? '');
        if (text) this.appendChild(new FakeText(text));
    }
}

function descendants(node) {
    return (node.children || []).flatMap(child => [child, ...descendants(child)]);
}

function loadPlayNotify(document) {
    const snippet = extract('    function playNotifiy(', '    let menuRegistry');
    return vm.runInNewContext(`(() => {
        ${snippet}
        return playNotifiy;
    })()`, { document, setTimeout: () => 1 });
}

function loadAddOpenFolderElement(deps) {
    const snippet = extract('    async function _addOpenFolderElement', '    async function addFileNameElement');
    return vm.runInNewContext(`(() => {
        const { config, sleep, getVisibleElement, allItemDataCache, logger, sendDataToLocalServer, document } = deps;
        ${snippet}
        return _addOpenFolderElement;
    })()`, { deps });
}

function loadCreateFileNameElement(document) {
    const snippet = extract('    function createFileNameElement', '    async function _addOpenFolderElement');
    return vm.runInNewContext(`(() => {
        ${snippet}
        return createFileNameElement;
    })()`, { document });
}

test('logger snapshots and redacts nested credentials without exposing original objects', () => {
    const { logger, calls } = loadLogger();
    const payload = {
        title: 'A normal title',
        API_KEY: 'SECRET_API_KEY',
        accessToken: 'SECRET_ACCESS_TOKEN',
        access_token: 'SECRET_ACCESS_TOKEN_2',
        Authorization: 'Bearer SECRET_AUTH',
        Cookie: 'session=SECRET_COOKIE',
        'X-Emby-Token': 'SECRET_EMBY',
        'x-plex-token': 'SECRET_PLEX',
        '%61ccess%5Ftoken': 'SECRET_ENCODED_KEY',
        sessionId: 'SECRET_SESSION',
        nested: [{ stage: 'playing', password: 'SECRET_NESTED_PASSWORD' }],
    };
    payload.circular = payload;
    const original = JSON.stringify({ ...payload, circular: '[self]' });
    const error = new Error('request failed https://user:SECRET_ERROR_PASS@example.test/item?token=SECRET_ERROR_TOKEN');
    error.stack = 'Error: failed Authorization: Bearer SECRET_STACK_TOKEN';
    const url = new URL('https://user:SECRET_URL_PASS@example.test/item?api_key=SECRET_URL_KEY&next=https%3A%2F%2Fnested.test%2F%3Ftoken%3DSECRET_NESTED_URL');
    const header = 'Authorization: Bearer SECRET_HEADER; X-Plex-Token=SECRET_HEADER_PLEX';

    assert.doesNotThrow(() => logger.info('Playing', payload, error, url, header));
    const output = stringifyCalls(calls);
    for (const secret of [
        'SECRET_API_KEY',
        'SECRET_ACCESS_TOKEN',
        'SECRET_ACCESS_TOKEN_2',
        'SECRET_AUTH',
        'SECRET_COOKIE',
        'SECRET_EMBY',
        'SECRET_PLEX',
        'SECRET_ENCODED_KEY',
        'SECRET_SESSION',
        'SECRET_NESTED_PASSWORD',
        'SECRET_ERROR_PASS',
        'SECRET_ERROR_TOKEN',
        'SECRET_STACK_TOKEN',
        'SECRET_URL_PASS',
        'SECRET_URL_KEY',
        'SECRET_NESTED_URL',
        'SECRET_HEADER',
        'SECRET_HEADER_PLEX',
    ]) {
        assert.equal(output.includes(secret), false, `secret leaked: ${secret}`);
    }
    assert.equal(output.includes('A normal title'), true);
    assert.equal(output.includes('playing'), true);
    assert.notStrictEqual(calls[0][2], payload);
    assert.notStrictEqual(calls[0][2].nested, payload.nested);
    assert.equal(JSON.stringify({ ...payload, circular: '[self]' }), original);
});

test('logger handles cycles, deep values, throwing getters, and proxies without throwing', () => {
    const { logger, calls } = loadLogger();
    const cycle = { name: 'cycle' };
    cycle.self = cycle;
    let deep = {};
    const deepRoot = deep;
    for (let index = 0; index < 20; index += 1) {
        deep.next = {};
        deep = deep.next;
    }
    const getter = {};
    Object.defineProperty(getter, 'boom', {
        enumerable: true,
        get() {
            throw new Error('getter should be omitted');
        },
    });
    const proxy = new Proxy({}, { ownKeys() { throw new Error('proxy should be omitted'); } });

    assert.doesNotThrow(() => logger.debug(cycle, deepRoot, getter, proxy));
    const output = stringifyCalls(calls);
    assert.equal(output.includes('[Circular]'), true);
    assert.equal(output.includes('[MaxDepth]'), true);
    assert.equal(output.includes('getter should be omitted'), false);
});

test('play notification keeps static markup and inserts dynamic values as text', () => {
    const body = new FakeElement('body');
    const head = new FakeElement('head');
    const document = {
        body,
        head,
        createElement: tagName => new FakeElement(tagName),
        createTextNode: value => new FakeText(value),
        getElementById: id => [...descendants(head), ...descendants(body)].find(node => node.id === id) || null,
    };
    const playNotifiy = loadPlayNotify(document);
    const title = '<img src=x onerror="globalThis.__unsafe=1">';
    const subtitle = 'Episode & <svg onload="globalThis.__unsafe=1">';
    playNotifiy(title, subtitle);

    const notification = body.children.at(-1);
    assert.ok(notification);
    assert.match(notification.innerHTML, /<svg/);
    assert.equal(notification.innerHTML.includes(title), false);
    assert.equal(notification.textContent, title + subtitle);
    assert.equal(descendants(notification).some(node => node.tagName === 'IMG' || node.tagName === 'SVG' && node !== notification), false);
    const dynamicNodes = descendants(notification).filter(node => node.tagName === 'DIV');
    assert.equal(dynamicNodes.some(node => node.textContent === title), true);
    assert.equal(dynamicNodes.some(node => node.textContent === subtitle), true);
});

test('strm path appends a break and text node while retaining existing nodes and events', async () => {
    const document = {
        createElement: tagName => new FakeElement(tagName),
        createTextNode: value => new FakeText(value),
        querySelectorAll: () => [],
    };
    const mediaSources = new FakeElement('div');
    const pathDiv = new FakeElement('div');
    pathDiv.className = 'sectionTitle sectionTitle-cards';
    const existing = new FakeElement('span');
    existing.textContent = 'https://server.example/movie.strm';
    const existingListener = () => {};
    existing.addEventListener('click', existingListener);
    pathDiv.appendChild(existing);
    const button = new FakeElement('a');
    pathDiv.insertAdjacentHTML = () => {};
    mediaSources.querySelector = selector => {
        if (selector.startsWith('div[class^=')) return pathDiv;
        if (selector === 'a#openFolderButton') return button;
        return null;
    };
    document.querySelectorAll = () => [mediaSources];
    const sent = [];
    const addOpenFolderElement = loadAddOpenFolderElement({
        config: { disableOpenFolder: false },
        sleep: async () => {},
        getVisibleElement: elements => elements[0],
        allItemDataCache: { item1: { Path: 'https://cdn.example/movie.strm?token=SECRET_PATH_TOKEN' } },
        logger: { info: () => {} },
        sendDataToLocalServer: (data, route) => sent.push({ data, route }),
        document,
    });

    await addOpenFolderElement('item1');
    assert.strictEqual(pathDiv.children[0], existing);
    assert.deepEqual(existing.listeners.click, [existingListener]);
    assert.equal(pathDiv.children[1].tagName, 'BR');
    assert.equal(pathDiv.children[2].nodeValue, 'https://cdn.example/movie.strm?token=SECRET_PATH_TOKEN');
    assert.equal(button.listeners.click.length, 1);
    button.listeners.click[0]();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].route, 'openFolder');
    assert.equal(sent[0].data.full_path, 'https://cdn.example/movie.strm?token=SECRET_PATH_TOKEN');
});

test('file name metadata is inserted as text nodes', () => {
    const document = {
        createElement: tagName => new FakeElement(tagName),
        createTextNode: value => new FakeText(value),
    };
    const createFileNameElement = loadCreateFileNameElement(document);
    const element = createFileNameElement(
        '<img src=x onerror="globalThis.__unsafe=4">',
        '<svg onload="globalThis.__unsafe=5">/media/movie.strm',
        true,
    );
    assert.equal(element.id, 'addFileNameElement');
    assert.equal(element.children[0].nodeType, 3);
    assert.equal(element.children[0].nodeValue.startsWith('<img'), true);
    assert.equal(element.children[1].tagName, 'BR');
    assert.equal(element.children[2].nodeType, 3);
    assert.equal(element.children[2].nodeValue.startsWith('<svg'), true);
});
