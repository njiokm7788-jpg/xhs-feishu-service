// ==UserScript==
// @name         小红书笔记 → 飞书抓取
// @namespace    https://github.com/xhs-feishu
// @version      1.0.0
// @description  小红书笔记页面一键提交链接到 XHS Feishu Service 抓取笔记内容
// @author       GenericAgent
// @match        https://www.xiaohongshu.com/explore/*
// @match        https://www.xiaohongshu.com/discovery/item/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      *
// ==/UserScript==

(function () {
  'use strict';

  // ── 持久化存储 key ──
  const STORE = {
    SERVICE_URL: 'xhs_feishu_service_url',
    PASSWORD: 'xhs_feishu_password',
    TOKEN: 'xhs_feishu_token',
  };

  // ── 默认配置 ──
  let config = {
    serviceUrl: GM_getValue(STORE.SERVICE_URL, ''),
    password: GM_getValue(STORE.PASSWORD, ''),
  };
  let cachedToken = GM_getValue(STORE.TOKEN, '');

  // ── 状态 ──
  let isSubmitting = false;
  let panelVisible = true;

  // ── 样式 ──
  GM_addStyle(`
    #xhs-feishu-panel {
      position: fixed;
      top: 100px;
      left: calc(100vw - 320px);
      z-index: 99999;
      background: #fff;
      border: 1px solid #e0e0e0;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.12);
      padding: 16px;
      width: 300px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      color: #333;
      user-select: none;
    }
    #xhs-feishu-panel.dragging {
      transition: none;
      box-shadow: 0 8px 32px rgba(0,0,0,0.2);
    }
    #xhs-feishu-panel.minimized {
      width: auto;
      padding: 10px 14px;
    }
    #xhs-feishu-panel .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
      cursor: move;
      user-select: none;
    }
    #xhs-feishu-panel .panel-header .panel-toggle {
      cursor: pointer;
    }
    #xhs-feishu-panel .panel-title {
      font-weight: 600;
      font-size: 14px;
      color: #ff2e4c;
    }
    #xhs-feishu-panel .panel-toggle {
      background: none;
      border: none;
      font-size: 16px;
      cursor: pointer;
      color: #999;
      line-height: 1;
      padding: 0 2px;
    }
    #xhs-feishu-panel .panel-body {
      display: block;
    }
    #xhs-feishu-panel.minimized .panel-body {
      display: none;
    }
    #xhs-feishu-panel label {
      display: block;
      font-size: 12px;
      color: #666;
      margin-bottom: 4px;
      margin-top: 10px;
    }
    #xhs-feishu-panel label:first-of-type {
      margin-top: 0;
    }
    #xhs-feishu-panel input,
    #xhs-feishu-panel select {
      width: 100%;
      box-sizing: border-box;
      padding: 8px 10px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font-size: 12px;
      outline: none;
      transition: border-color 0.2s;
    }
    #xhs-feishu-panel input:focus,
    #xhs-feishu-panel select:focus {
      border-color: #ff2e4c;
    }
    #xhs-feishu-panel .current-url {
      margin-top: 10px;
      font-size: 11px;
      color: #999;
      word-break: break-all;
      max-height: 32px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      background: #f8f8f8;
      padding: 6px 8px;
      border-radius: 4px;
    }
    #xhs-feishu-panel .btn-submit {
      margin-top: 12px;
      width: 100%;
      padding: 10px;
      background: #ff2e4c;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s, opacity 0.2s;
    }
    #xhs-feishu-panel .btn-submit:hover {
      background: #e6253f;
    }
    #xhs-feishu-panel .btn-submit:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    #xhs-feishu-panel .btn-save {
      margin-top: 6px;
      width: 100%;
      padding: 6px;
      background: #f5f5f5;
      color: #666;
      border: 1px solid #ddd;
      border-radius: 6px;
      font-size: 12px;
      cursor: pointer;
    }
    #xhs-feishu-panel .btn-save:hover {
      background: #eee;
    }
    #xhs-feishu-panel .status {
      margin-top: 10px;
      padding: 8px;
      border-radius: 6px;
      font-size: 12px;
      text-align: center;
      display: none;
    }
    #xhs-feishu-panel .status.success {
      display: block;
      background: #e8f5e9;
      color: #2e7d32;
    }
    #xhs-feishu-panel .status.error {
      display: block;
      background: #ffebee;
      color: #c62828;
    }
    #xhs-feishu-panel .status.info {
      display: block;
      background: #e3f2fd;
      color: #1565c0;
    }
  `);

  // ── 工具函数 ──
  function getCurrentUrl() {
    // 小红书笔记 URL，直接拿 location.href
    let url = location.href;
    // 去掉可能的 query 参数（?xsec_token 等），保留完整链接给服务处理
    // 但服务 README 说保留原始链接（含 xsec_token），所以直接传完整 URL
    return url;
  }

  function showStatus(type, msg) {
    const el = document.getElementById('xhs-feishu-status');
    if (!el) return;
    el.className = 'status ' + type;
    el.textContent = msg;
    el.style.display = 'block';
    if (type === 'success' || type === 'error') {
      setTimeout(() => {
        el.style.display = 'none';
        el.className = 'status';
      }, 5000);
    }
  }

  function clearStatus() {
    const el = document.getElementById('xhs-feishu-status');
    if (el) {
      el.style.display = 'none';
      el.className = 'status';
    }
  }

  // ── 登录获取 token ──
  function doLogin(callback) {
    const serviceUrl = config.serviceUrl.trim().replace(/\/+$/, '');
    const loginUrl = serviceUrl + '/auth/login';
    GM_xmlhttpRequest({
      method: 'POST',
      url: loginUrl,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ password: config.password }),
      onload: function (resp) {
        try {
          const data = JSON.parse(resp.responseText);
          if (resp.status === 200 && data.ok) {
            cachedToken = data.token;
            GM_setValue(STORE.TOKEN, cachedToken);
            callback(cachedToken);
          } else {
            const detail = data.detail || '密码错误';
            callback(null, detail);
          }
        } catch (e) {
          callback(null, '登录响应解析失败');
        }
      },
      onerror: function () {
        callback(null, '登录请求失败，请检查服务地址');
      },
      ontimeout: function () {
        callback(null, '登录超时');
      },
      timeout: 10000,
    });
  }

  // ── 提交 ──
  function submitNote() {
    if (isSubmitting) return;

    const serviceUrl = config.serviceUrl.trim().replace(/\/+$/, '');
    const pwd = config.password.trim();

    if (!serviceUrl) {
      showStatus('error', '请先填写服务地址');
      return;
    }
    if (!pwd) {
      showStatus('error', '请先填写前端密码');
      return;
    }

    const url = getCurrentUrl();
    if (!url) {
      showStatus('error', '无法获取当前页面链接');
      return;
    }

    isSubmitting = true;
    const btn = document.getElementById('xhs-feishu-btn-submit');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '登录中…';
    }
    clearStatus();

    // 先登录获取 token，再提交
    doLogin(function (token, err) {
      if (err) {
        isSubmitting = false;
        if (btn) { btn.disabled = false; btn.textContent = '提交抓取'; }
        showStatus('error', '❌ 登录失败: ' + err);
        return;
      }
      if (btn) btn.textContent = '提交中…';

      const apiUrl = serviceUrl + '/extract-note-submit';
      GM_xmlhttpRequest({
        method: 'POST',
        url: apiUrl,
        headers: {
          'Content-Type': 'application/json',
          'X-Frontend-Token': token,
        },
        data: JSON.stringify({ url: url }),
        onload: function (resp) {
          isSubmitting = false;
          if (btn) { btn.disabled = false; btn.textContent = '提交抓取'; }
          try {
            const data = JSON.parse(resp.responseText);
            if (resp.status === 200 && data.ok) {
              const note = data.note || {};
              const title = note.title || '未知标题';
              showStatus('success', '✅ 提交成功！笔记「' + title + '」已写入飞书');
            } else {
              const detail = data.detail || data.error || resp.statusText;
              showStatus('error', '❌ 提交失败: ' + detail);
            }
          } catch (e) {
            showStatus('error', '❌ 响应解析失败 (' + resp.status + ')');
          }
        },
        onerror: function () {
          isSubmitting = false;
          if (btn) { btn.disabled = false; btn.textContent = '提交抓取'; }
          showStatus('error', '❌ 网络错误，请检查服务地址');
        },
        ontimeout: function () {
          isSubmitting = false;
          if (btn) { btn.disabled = false; btn.textContent = '提交抓取'; }
          showStatus('error', '❌ 请求超时（抓取可能需要较长时间）');
        },
        timeout: 120000,
      });
    });
  }

  function saveConfig() {
    const urlInput = document.getElementById('xhs-feishu-url');
    const keyInput = document.getElementById('xhs-feishu-key');
    config.serviceUrl = urlInput ? urlInput.value.trim() : config.serviceUrl;
    config.password = keyInput ? keyInput.value.trim() : config.password;

    // 密码变更时清除旧 token
    const oldPassword = GM_getValue(STORE.PASSWORD, '');
    if (config.password !== oldPassword) {
      cachedToken = '';
      GM_setValue(STORE.TOKEN, '');
    }

    GM_setValue(STORE.SERVICE_URL, config.serviceUrl);
    GM_setValue(STORE.PASSWORD, config.password);
    showStatus('info', '配置已保存');
  }

  // ── 构建面板 ──
  function buildPanel() {
    const panel = document.createElement('div');
    panel.id = 'xhs-feishu-panel';
    panel.innerHTML = `
      <div class="panel-header" id="xhs-feishu-header">
        <span class="panel-title">📕 提交飞书</span>
        <button class="panel-toggle" id="xhs-feishu-toggle">−</button>
      </div>
      <div class="panel-body">
        <label>服务地址</label>
        <input type="text" id="xhs-feishu-url" placeholder="https://xxx.onrender.com" value="${escapeHTML(config.serviceUrl)}">
        <label>前端密码</label>
        <input type="password" id="xhs-feishu-key" placeholder="输入 FRONTEND_PASSWORD" value="${escapeHTML(config.password)}">
        <div class="current-url" id="xhs-feishu-current-url" title="${escapeHTML(getCurrentUrl())}">🔗 ${escapeHTML(getCurrentUrl())}</div>
        <button class="btn-save" id="xhs-feishu-btn-save">💾 保存配置</button>
        <button class="btn-submit" id="xhs-feishu-btn-submit">🚀 提交抓取</button>
        <div class="status" id="xhs-feishu-status"></div>
      </div>
    `;
    document.body.appendChild(panel);

    // 绑定事件
    document.getElementById('xhs-feishu-btn-submit').addEventListener('click', submitNote);
    document.getElementById('xhs-feishu-btn-save').addEventListener('click', saveConfig);

    // 折叠/展开（仅 toggle 按钮）
    document.getElementById('xhs-feishu-toggle').addEventListener('click', function (e) {
      e.stopPropagation();
      panelVisible = !panelVisible;
      if (panelVisible) {
        panel.classList.remove('minimized');
        this.textContent = '−';
      } else {
        panel.classList.add('minimized');
        this.textContent = '+';
      }
    });

    // ── 拖拽移动 ──
    const header = document.getElementById('xhs-feishu-header');
    let dragInfo = { active: false, startX: 0, startY: 0, startLeft: 0, startTop: 0 };

    header.addEventListener('mousedown', function (e) {
      if (e.target === document.getElementById('xhs-feishu-toggle')) return;
      if (panel.classList.contains('minimized')) return;
      e.preventDefault();
      dragInfo.active = true;
      dragInfo.startX = e.clientX;
      dragInfo.startY = e.clientY;
      dragInfo.startLeft = panel.offsetLeft;
      dragInfo.startTop = panel.offsetTop;
      panel.classList.add('dragging');
    });

    document.addEventListener('mousemove', function (e) {
      if (!dragInfo.active) return;
      const dx = e.clientX - dragInfo.startX;
      const dy = e.clientY - dragInfo.startY;
      let newLeft = dragInfo.startLeft + dx;
      let newTop = dragInfo.startTop + dy;
      // 边界限制，防止拖出屏幕
      newLeft = Math.max(0, Math.min(newLeft, window.innerWidth - panel.offsetWidth));
      newTop = Math.max(0, Math.min(newTop, window.innerHeight - 40));
      panel.style.left = newLeft + 'px';
      panel.style.top = newTop + 'px';
    });

    document.addEventListener('mouseup', function () {
      if (!dragInfo.active) return;
      dragInfo.active = false;
      panel.classList.remove('dragging');
    });

    // 实时更新 URL 预览（SPA 页面）
    setInterval(() => {
      const el = document.getElementById('xhs-feishu-current-url');
      if (el) {
        const cur = getCurrentUrl();
        el.textContent = '🔗 ' + cur;
        el.title = cur;
      }
    }, 2000);
  }

  function escapeHTML(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ── 启动 ──
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', buildPanel);
  } else {
    buildPanel();
  }
})();