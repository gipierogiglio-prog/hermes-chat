/**
 * Hermes Chat - Frontend App
 * SPA with Google Auth, group chat, and WebSocket real-time
 */
(() => {
  'use strict';

  // --- State ---
  const state = {
    user: null,
    groups: [],
    activeGroupId: null,
    ws: null,
    wsReconnectTimer: null,
    loadingMessages: false,
    hasMoreMessages: true,
    firstMessageId: null,
  };

  // --- DOM Refs ---
  const $ = (sel) => document.querySelector(sel);
  const loginScreen = $('#login-screen');
  const mainApp = $('#main-app');
  const groupList = $('#group-list');
  const userAvatar = $('#user-avatar');
  const userName = $('#user-name');
  const emptyState = $('#empty-state');
  const activeChat = $('#active-chat');
  const chatGroupName = $('#chat-group-name');
  const chatGroupContext = $('#chat-group-context');
  const messagesContainer = $('#messages-container');
  const typingIndicator = $('#typing-indicator');
  const messageInput = $('#message-input');
  const sendBtn = $('#send-btn');
  const logoutBtn = $('#logout-btn');
  const newGroupBtn = $('#new-group-btn');
  const inviteInfoBtn = $('#invite-info-btn');
  const copyInviteBtn = $('#copy-invite-btn');
  const viewVaultBtn = $('#view-vault-btn');
  const editContextBtn = $('#edit-context-btn');
  const toast = $('#toast');

  // Modals
  const modalOverlay = $('#modal-overlay');
  const newGroupModal = $('#new-group-modal');
  const groupNameInput = $('#group-name-input');
  const groupContextInput = $('#group-context-input');
  const modalCreate = $('#modal-create');
  const modalCancel = $('#modal-cancel');

  const editContextModal = $('#edit-context-modal');
  const editContextInput = $('#edit-context-input');
  const editSave = $('#edit-save');
  const editCancel = $('#edit-cancel');

  // --- Helpers ---
  function showToast(msg, duration = 3000) {
    toast.textContent = msg;
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), duration);
  }

  function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  }

  function getCookie(name) {
    const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return match ? decodeURIComponent(match[1]) : null;
  }

  // --- API Client ---
  async function api(url, options = {}) {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...options.headers },
      ...options,
    });
    if (resp.status === 401) {
      window.location.reload();
      return null;
    }
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `HTTP ${resp.status}`);
    }
    return resp.json();
  }

  // --- Auth ---
  async function checkAuth() {
    try {
      const data = await api('/auth/me');
      if (data?.authenticated) {
        state.user = data.user;
        userAvatar.src = data.user.picture || '';
        userName.textContent = data.user.name;
        loginScreen.classList.add('hidden');
        mainApp.classList.remove('hidden');
        loadGroups();
        return true;
      }
    } catch (e) { /* not authenticated */ }
    loginScreen.classList.remove('hidden');
    mainApp.classList.add('hidden');
    return false;
  }

  // --- Groups ---
  async function loadGroups() {
    groupList.innerHTML = '<div class="loading">Carregando grupos...</div>';
    try {
      state.groups = await api('/api/groups');
      renderGroupList();
    } catch (e) {
      groupList.innerHTML = `<div class="loading">Erro: ${e.message}</div>`;
    }
  }

  function renderGroupList() {
    if (state.groups.length === 0) {
      groupList.innerHTML = `
        <div class="loading" style="padding: 30px 20px;">
          <p style="margin-bottom: 8px;">Nenhum grupo ainda</p>
          <p style="font-size: 13px;">Clique em ＋ para criar</p>
        </div>`;
      return;
    }
    groupList.innerHTML = state.groups.map(g => `
      <div class="group-item ${g.id === state.activeGroupId ? 'active' : ''}"
           data-id="${g.id}"
           onclick="__app.selectGroup('${g.id}')">
        <div class="group-item-name">${escHtml(g.name)}</div>
        <div class="group-item-context">${escHtml(g.context || 'Sem contexto')}</div>
        <div class="group-item-info">
          <span>${g.msg_count || 0} msgs</span>
          <span>${g.role === 'admin' ? '👑' : 'Membro'}</span>
        </div>
      </div>
    `).join('');
  }

  async function createGroup(name, context) {
    try {
      const g = await api('/api/groups', {
        method: 'POST',
        body: JSON.stringify({ name, context }),
      });
      showToast(`Grupo "${g.name}" criado!`);
      await loadGroups();
      selectGroup(g.id);
    } catch (e) {
      showToast(`Erro: ${e.message}`);
    }
  }

  function selectGroup(groupId) {
    state.activeGroupId = groupId;
    state.firstMessageId = null;
    state.hasMoreMessages = true;
    disconnectWs();

    renderGroupList();
    emptyState.classList.add('hidden');
    activeChat.classList.remove('hidden');

    loadGroupInfo(groupId);
    loadMessages(groupId);
    connectWs(groupId);
  }

  async function loadGroupInfo(groupId) {
    try {
      const g = await api(`/api/groups/${groupId}`);
      chatGroupName.textContent = g.name;
      chatGroupContext.textContent = g.context || 'Sem contexto definido';
      chatGroupContext.title = g.context || '';
      editContextBtn.style.display = g.role === 'admin' ? '' : 'none';
      viewVaultBtn.dataset.vaultPath = g.vault_path || '';
    } catch (e) {
      showToast(`Erro: ${e.message}`);
    }
  }

  // --- Messages ---
  async function loadMessages(groupId, beforeId) {
    state.loadingMessages = true;
    try {
      const url = beforeId
        ? `/api/groups/${groupId}/messages?before=${beforeId}&limit=50`
        : `/api/groups/${groupId}/messages?limit=50`;

      const msgs = await api(url);
      state.hasMoreMessages = msgs.length >= 50;

      if (beforeId) {
        // Prepend older messages
        msgs.forEach(m => {
          const el = createMessageElement(m);
          messagesContainer.insertBefore(el, messagesContainer.firstChild);
        });
        if (msgs.length > 0) {
          state.firstMessageId = msgs[0].id;
        }
        // Keep scroll position
      } else {
        messagesContainer.innerHTML = '';
        msgs.forEach(m => messagesContainer.appendChild(createMessageElement(m)));
        if (msgs.length > 0) {
          state.firstMessageId = msgs[0].id;
        }
        setTimeout(() => {
          messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }, 50);
      }
    } catch (e) {
      showToast(`Erro ao carregar msgs: ${e.message}`);
    }
    state.loadingMessages = false;
  }

  function createMessageElement(msg) {
    const div = document.createElement('div');
    div.dataset.msgId = msg.id || '';

    if (msg.is_hermes) {
      div.className = 'message hermes';
      div.innerHTML = `
        <div class="msg-author">🤖 Hermes</div>
        <div class="msg-content">${renderContent(msg.content)}</div>
        <div class="msg-time">${formatTime(msg.created_at)}</div>`;
    } else if (msg.user_id === '__system__') {
      div.className = 'message system';
      div.innerHTML = `<div class="msg-content">${escHtml(msg.content)}</div>`;
    } else {
      div.className = 'message user';
      div.innerHTML = `
        <div class="msg-author">${escHtml(msg.user_name || 'Alguém')}</div>
        <div class="msg-content">${renderContent(msg.content)}</div>
        <div class="msg-time">${formatTime(msg.created_at)}</div>`;
    }
    return div;
  }

  function renderContent(text) {
    if (!text) return '';
    let html = escHtml(text)
      .replace(/```(\w*)\n?([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
      .replace(/\n/g, '<br>');
    return html;
  }

  // --- WebSocket ---
  function connectWs(groupId) {
    disconnectWs();
    const token = getCookie('session');
    if (!token) return;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/${groupId}?token=${encodeURIComponent(token)}`;

    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => console.log('WS connected');

    state.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        typingIndicator.classList.add('hidden');
        switch (data.type) {
          case 'hermes_message':
            messagesContainer.appendChild(createMessageElement({
              content: data.content,
              id: data.id,
              is_hermes: true,
              created_at: data.timestamp,
            }));
            scrollDown();
            break;
          case 'hermes_typing':
            typingIndicator.classList.remove('hidden');
            scrollDown();
            break;
        }
      } catch (e) { console.error('WS parse:', e); }
    };

    state.ws.onclose = () => {
      state.ws = null;
      if (state.activeGroupId === groupId) {
        state.wsReconnectTimer = setTimeout(() => connectWs(groupId), 3000);
      }
    };
    state.ws.onerror = () => state.ws?.close();
  }

  function disconnectWs() {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
      state.ws = null;
    }
  }

  function sendWsMessage(content) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      showToast('Reconectando...');
      connectWs(state.activeGroupId);
      return false;
    }
    state.ws.send(JSON.stringify({ type: 'message', content }));
    return true;
  }

  function scrollDown() {
    setTimeout(() => { messagesContainer.scrollTop = messagesContainer.scrollHeight; }, 50);
  }

  // --- Infinite Scroll (load older messages) ---
  messagesContainer.addEventListener('scroll', () => {
    if (messagesContainer.scrollTop < 100 && !state.loadingMessages && state.hasMoreMessages && state.firstMessageId) {
      loadMessages(state.activeGroupId, state.firstMessageId);
    }
  });

  // --- Input Events ---
  messageInput.addEventListener('input', () => {
    sendBtn.disabled = !messageInput.value.trim();
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
  });

  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  sendBtn.addEventListener('click', sendMessage);

  function sendMessage() {
    const text = messageInput.value.trim();
    if (!text || !state.activeGroupId) return;

    // Optimistic UI
    messagesContainer.appendChild(createMessageElement({
      content: text,
      user_name: state.user?.name || 'Você',
      is_hermes: false,
      created_at: new Date().toISOString(),
    }));
    scrollDown();

    messageInput.value = '';
    sendBtn.disabled = true;
    messageInput.style.height = 'auto';
    sendWsMessage(text);
  }

  // --- Modals ---
  newGroupBtn.addEventListener('click', () => {
    groupNameInput.value = '';
    groupContextInput.value = '';
    newGroupModal.classList.remove('hidden');
    editContextModal.classList.add('hidden');
    modalOverlay.classList.remove('hidden');
    setTimeout(() => groupNameInput.focus(), 100);
  });

  modalCancel.addEventListener('click', closeModal);
  (document.querySelector('#new-group-modal .modal-close') || modalCancel).addEventListener('click', closeModal);

  modalCreate.addEventListener('click', async () => {
    const name = groupNameInput.value.trim();
    if (!name) { showToast('Nome obrigatório'); return; }
    closeModal();
    await createGroup(name, groupContextInput.value.trim());
  });

  editContextBtn.addEventListener('click', async () => {
    try {
      const g = await api(`/api/groups/${state.activeGroupId}`);
      editContextInput.value = g.context || '';
      editContextModal.dataset.groupId = state.activeGroupId;
      editContextModal.classList.remove('hidden');
      newGroupModal.classList.add('hidden');
      modalOverlay.classList.remove('hidden');
      setTimeout(() => editContextInput.focus(), 100);
    } catch (e) { showToast(`Erro: ${e.message}`); }
  });

  editCancel.addEventListener('click', closeModal);
  (document.querySelector('#edit-context-modal .modal-close') || editCancel).addEventListener('click', closeModal);

  editSave.addEventListener('click', async () => {
    const groupId = editContextModal.dataset.groupId;
    const context = editContextInput.value.trim();
    try {
      await api(`/api/groups/${groupId}`, { method: 'PATCH', body: JSON.stringify({ context }) });
      closeModal();
      loadGroupInfo(groupId);
      showToast('Contexto atualizado!');
    } catch (e) { showToast(`Erro: ${e.message}`); }
  });

  function closeModal() {
    modalOverlay.classList.add('hidden');
    newGroupModal.classList.add('hidden');
    editContextModal.classList.add('hidden');
  }
  modalOverlay.addEventListener('click', (e) => { if (e.target === modalOverlay) closeModal(); });

  // --- Actions ---
  copyInviteBtn.addEventListener('click', () => {
    const url = `${window.location.origin}/?join=${state.activeGroupId}`;
    navigator.clipboard.writeText(url).then(
      () => showToast('Link do grupo copiado!'),
      () => { document.execCommand('copy'); showToast('Link copiado!'); }
    );
  });

  viewVaultBtn.addEventListener('click', () => {
    const path = viewVaultBtn.dataset.vaultPath;
    showToast(path ? `📁 ${path}` : 'Vault não disponível');
  });

  inviteInfoBtn.addEventListener('click', () => {
    showToast('🔗 Abra um grupo e clique em 🔗 no cabeçalho para copiar o link de convite', 5000);
  });

  logoutBtn.addEventListener('click', async () => {
    await api('/auth/logout', { method: 'POST' });
    window.location.reload();
  });

  // --- Join by invite link ---
  async function handleInviteLink() {
    const params = new URLSearchParams(window.location.search);
    const joinId = params.get('join');
    if (!joinId) return;
    await new Promise(resolve => {
      const t = setInterval(() => { if (state.user) { clearInterval(t); resolve(); } }, 200);
      setTimeout(() => { clearInterval(t); resolve(); }, 10000);
    });
    try {
      await api(`/api/groups/${joinId}/join`, { method: 'POST' });
      showToast('Você entrou no grupo!');
      await loadGroups();
      selectGroup(joinId);
    } catch (e) { showToast(`Erro: ${e.message}`); }
    window.history.replaceState({}, '', window.location.pathname);
  }

  // --- Init ---
  window.__app = { selectGroup };

  async function init() {
    await checkAuth();
    if (state.user) await handleInviteLink();
  }
  init();
})();
