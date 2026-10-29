/**
 * 截图笔记前端逻辑（原生 JS，无第三方依赖）。
 * 接口契约见 API.md：
 *   GET    /api/notes
 *   POST   /api/notes            { image: dataURL }
 *   PUT    /api/notes/{id}       { title, content }
 *   DELETE /api/notes/{id}
 * 错误响应统一为 { "error": "..." }。
 */
(function () {
  'use strict';

  var MAX_FILE_BYTES = 20 * 1024 * 1024;
  var SUMMARY_LIMIT = 180;
  var IMAGE_MIME_TO_EXT = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'image/bmp': 'bmp'
  };
  var IMAGE_EXT_RE = /\.(png|jpe?g|webp|gif|bmp)$/i;

  var DATE_TIME_FORMATTER = (function () {
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
      });
    } catch (error) {
      return null;
    }
  })();

  var state = {
    notes: [],
    loading: false,
    loadError: '',
    uploading: false,
    currentNote: null,
    lastFocused: null,
    uploadStatusTimer: 0
  };

  var dom = {};

  function ApiError(message, status) {
    this.name = 'ApiError';
    this.message = message;
    this.status = typeof status === 'number' ? status : 0;
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, ApiError);
    } else {
      this.stack = new Error(message).stack;
    }
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /* ---------- DOM 缓存与启动 ---------- */

  function cacheDom() {
    dom.dropzone = document.getElementById('dropzone');
    dom.fileInput = document.getElementById('file-input');
    dom.uploadStatus = document.getElementById('upload-status');
    dom.notesList = document.getElementById('notes-list');
    dom.noteCount = document.getElementById('note-count');
    dom.overlay = document.getElementById('detail-overlay');
    dom.detailImage = document.getElementById('detail-image');
    dom.detailImageError = document.getElementById('detail-image-error');
    dom.detailTitle = document.getElementById('detail-title');
    dom.detailContent = document.getElementById('detail-content');
    dom.detailTime = document.getElementById('detail-time');
    dom.detailSave = document.getElementById('detail-save');
    dom.detailDelete = document.getElementById('detail-delete');
    dom.detailClose = document.getElementById('detail-close');
    dom.detailCancel = document.getElementById('detail-cancel');
    dom.toastContainer = document.getElementById('toast-container');
  }

  function bindEvents() {
    dom.dropzone.addEventListener('click', onDropzoneActivate);
    dom.dropzone.addEventListener('keydown', onDropzoneKeydown);
    dom.fileInput.addEventListener('change', onFileInputChange);

    document.addEventListener('dragenter', onDocumentDragEnter);
    document.addEventListener('dragover', onDocumentDragOver);
    document.addEventListener('dragleave', onDocumentDragLeave);
    document.addEventListener('drop', onDocumentDrop);
    document.addEventListener('paste', onPaste);

    dom.detailImage.addEventListener('error', onDetailImageError);
    dom.detailSave.addEventListener('click', saveDetail);
    dom.detailDelete.addEventListener('click', deleteDetail);
    dom.detailClose.addEventListener('click', closeDetail);
    dom.detailCancel.addEventListener('click', closeDetail);
    document.addEventListener('keydown', onDocumentKeydown);
    window.addEventListener('blur', clearDragState);
  }

  function init() {
    cacheDom();
    bindEvents();
    loadNotes();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* ---------- 网络请求 ---------- */

  function requestJSON(path, options) {
    return fetch(path, options).then(function (response) {
      if (response.status === 204) {
        return null;
      }
      return response.text().then(function (text) {
        var payload = null;
        if (text) {
          try {
            payload = JSON.parse(text);
          } catch (error) {
            payload = null;
          }
        }
        if (!response.ok) {
          var message = payload && typeof payload.error === 'string' && payload.error.trim()
            ? payload.error.trim()
            : '请求失败（HTTP ' + response.status + '）';
          throw new ApiError(message, response.status);
        }
        return payload;
      });
    }, function () {
      throw new ApiError('网络连接失败，请确认本地服务正在运行', 0);
    });
  }

  function jsonRequestOptions(method, body) {
    return {
      method: method,
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body)
    };
  }

  function readFileAsDataURL(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        if (typeof reader.result === 'string') {
          resolve(reader.result);
        } else {
          reject(new ApiError('读取图片失败，请重试', 0));
        }
      };
      reader.onerror = function () {
        reject(new ApiError('读取图片失败，请重试', 0));
      };
      reader.readAsDataURL(file);
    });
  }

  function createNote(dataUrl) {
    return requestJSON('/api/notes', jsonRequestOptions('POST', { image: dataUrl })).then(function (data) {
      if (!data || !data.note) {
        throw new ApiError('服务器返回的数据格式异常', 0);
      }
      return data.note;
    });
  }

  /* ---------- 笔记列表 ---------- */

  function loadNotes() {
    state.loading = true;
    state.loadError = '';
    dom.notesList.setAttribute('aria-busy', 'true');
    renderNotesLoading();
    return requestJSON('/api/notes').then(function (data) {
      var notes = data && Array.isArray(data.notes) ? data.notes : [];
      state.notes = notes.filter(isNoteLike);
    }, function (error) {
      state.notes = [];
      state.loadError = error && error.message ? error.message : '加载失败';
    }).then(function () {
      state.loading = false;
      dom.notesList.setAttribute('aria-busy', 'false');
      renderNotes();
    });
  }

  function isNoteLike(note) {
    return note !== null && typeof note === 'object' && note.id !== undefined && note.id !== null && String(note.id) !== '';
  }

  function findNote(noteId) {
    var target = String(noteId);
    for (var i = 0; i < state.notes.length; i += 1) {
      if (String(state.notes[i].id) === target) {
        return state.notes[i];
      }
    }
    return null;
  }

  function upsertNote(note) {
    if (!note || note.id === undefined || note.id === null) {
      return;
    }
    var target = String(note.id);
    for (var i = 0; i < state.notes.length; i += 1) {
      if (String(state.notes[i].id) === target) {
        state.notes[i] = note;
        return;
      }
    }
    state.notes.unshift(note);
  }

  function clearNode(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function renderNotesLoading() {
    clearNode(dom.notesList);
    updateNoteCount();
    var block = document.createElement('div');
    block.className = 'state state-loading';
    block.setAttribute('role', 'status');
    block.textContent = '加载中…';
    dom.notesList.appendChild(block);
  }

  function renderNotes() {
    clearNode(dom.notesList);
    updateNoteCount();
    if (state.loadError) {
      dom.notesList.appendChild(createStateBlock(state.loadError, true));
      return;
    }
    if (state.notes.length === 0) {
      dom.notesList.appendChild(createStateBlock('还没有笔记。粘贴、拖拽或选择一张截图开始吧。', false));
      return;
    }
    var fragment = document.createDocumentFragment();
    for (var i = 0; i < state.notes.length; i += 1) {
      fragment.appendChild(createCard(state.notes[i]));
    }
    dom.notesList.appendChild(fragment);
  }

  function updateNoteCount() {
    dom.noteCount.textContent = state.notes.length > 0 ? state.notes.length + ' 条' : '';
  }

  function createStateBlock(message, canRetry) {
    var block = document.createElement('div');
    block.className = 'state';
    var text = document.createElement('p');
    text.className = 'state-text';
    text.textContent = message;
    block.appendChild(text);
    if (canRetry) {
      var retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'button';
      retry.textContent = '重试';
      retry.addEventListener('click', function () {
        loadNotes();
      });
      block.appendChild(retry);
    }
    return block;
  }

  function createCard(note) {
    var title = noteTitle(note);
    var card = document.createElement('article');
    card.className = 'card';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', '打开笔记：' + title);

    var thumb = document.createElement('div');
    thumb.className = 'thumb';
    if (note.image_url) {
      var image = document.createElement('img');
      image.className = 'thumb-image';
      image.src = note.image_url;
      image.alt = '';
      image.loading = 'lazy';
      image.decoding = 'async';
      image.addEventListener('error', function () {
        if (image.parentNode === thumb) {
          thumb.removeChild(image);
        }
        thumb.appendChild(createThumbFallback());
      });
      thumb.appendChild(image);
    } else {
      thumb.appendChild(createThumbFallback());
    }

    var body = document.createElement('div');
    body.className = 'card-body';

    var heading = document.createElement('h3');
    heading.className = 'card-title';
    heading.textContent = title;

    var summary = document.createElement('p');
    summary.className = 'card-summary';
    summary.textContent = noteSummary(note);

    var time = document.createElement('time');
    time.className = 'card-time';
    if (note.created_at) {
      time.dateTime = note.created_at;
    }
    time.textContent = formatDateTime(note.created_at);

    body.appendChild(heading);
    body.appendChild(summary);
    body.appendChild(time);
    card.appendChild(thumb);
    card.appendChild(body);

    card.addEventListener('click', function () {
      openDetail(note.id);
    });
    card.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        openDetail(note.id);
      }
    });
    return card;
  }

  function createThumbFallback() {
    var fallback = document.createElement('span');
    fallback.className = 'thumb-fallback';
    fallback.textContent = '无缩略图';
    return fallback;
  }

  function noteTitle(note) {
    if (note && typeof note.title === 'string' && note.title.trim()) {
      return note.title.trim();
    }
    return '无标题';
  }

  function noteSummary(note) {
    var content = note && typeof note.content === 'string' ? note.content.trim() : '';
    if (!content) {
      return '（无内容）';
    }
    var singleLine = content.replace(/\s+/g, ' ');
    if (singleLine.length > SUMMARY_LIMIT) {
      return singleLine.slice(0, SUMMARY_LIMIT) + '…';
    }
    return singleLine;
  }

  function formatDateTime(value) {
    if (!value) {
      return '';
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    if (DATE_TIME_FORMATTER) {
      try {
        return DATE_TIME_FORMATTER.format(date);
      } catch (error) {
        /* 回退到手工格式化 */
      }
    }
    var pad = function (number) {
      return String(number).padStart(2, '0');
    };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
      ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  /* ---------- 上传 ---------- */

  function isSupportedImage(file) {
    if (!file) {
      return false;
    }
    var type = (file.type || '').toLowerCase();
    if (IMAGE_MIME_TO_EXT[type]) {
      return true;
    }
    if (!type) {
      return IMAGE_EXT_RE.test(file.name || '');
    }
    return false;
  }

  function imageFilesFromDataTransfer(dataTransfer) {
    var files = [];
    var items = dataTransfer.items ? Array.prototype.slice.call(dataTransfer.items) : [];
    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      if (!item || item.kind !== 'file') {
        continue;
      }
      var type = (item.type || '').toLowerCase();
      if (!IMAGE_MIME_TO_EXT[type]) {
        continue;
      }
      var file = item.getAsFile ? item.getAsFile() : null;
      if (file) {
        files.push(file);
      }
    }
    if (files.length === 0 && dataTransfer.files && dataTransfer.files.length > 0) {
      files = Array.prototype.slice.call(dataTransfer.files).filter(isSupportedImage);
    }
    return files;
  }

  function onDropzoneActivate() {
    if (state.uploading) {
      return;
    }
    dom.fileInput.click();
  }

  function onDropzoneKeydown(event) {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
      event.preventDefault();
      onDropzoneActivate();
    }
  }

  function onFileInputChange() {
    var files = Array.prototype.slice.call(dom.fileInput.files || []);
    dom.fileInput.value = '';
    if (files.length > 0) {
      handleFiles(files);
    }
  }

  function dataTransferHasFiles(dataTransfer) {
    if (!dataTransfer || !dataTransfer.types) {
      return false;
    }
    return Array.prototype.indexOf.call(dataTransfer.types, 'Files') !== -1;
  }

  function onDocumentDragEnter(event) {
    if (!dataTransferHasFiles(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    if (!state.uploading) {
      dom.dropzone.classList.add('is-dragover');
    }
  }

  function onDocumentDragOver(event) {
    if (!dataTransferHasFiles(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
    if (!state.uploading) {
      dom.dropzone.classList.add('is-dragover');
    }
  }

  function onDocumentDragLeave(event) {
    if (!event.relatedTarget) {
      clearDragState();
    }
  }

  function clearDragState() {
    dom.dropzone.classList.remove('is-dragover');
  }

  function onDocumentDrop(event) {
    if (!event.dataTransfer) {
      return;
    }
    var files = Array.prototype.slice.call(event.dataTransfer.files || []);
    if (files.length === 0 && !dataTransferHasFiles(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    clearDragState();
    if (files.length === 0) {
      files = imageFilesFromDataTransfer(event.dataTransfer);
    }
    if (files.length > 0) {
      handleFiles(files);
    }
  }

  function onPaste(event) {
    if (state.uploading) {
      return;
    }
    var clipboard = event.clipboardData;
    if (!clipboard) {
      /* 极少数环境没有 clipboardData，降级读取异步剪贴板 API。 */
      readClipboardImagesFallback();
      return;
    }
    var files = imageFilesFromDataTransfer(clipboard);
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    handleFiles(files);
  }

  function readClipboardImagesFallback() {
    if (!navigator.clipboard || typeof navigator.clipboard.read !== 'function') {
      return;
    }
    navigator.clipboard.read().then(function (clipboardItems) {
      var files = [];
      var chain = Promise.resolve();
      clipboardItems.forEach(function (clipboardItem, index) {
        var types = clipboardItem.types || [];
        types.forEach(function (rawType) {
          var type = String(rawType).toLowerCase();
          if (!IMAGE_MIME_TO_EXT[type]) {
            return;
          }
          chain = chain.then(function () {
            return clipboardItem.getType(type).then(function (blob) {
              var extension = IMAGE_MIME_TO_EXT[type];
              var name = 'clipboard-' + Date.now() + '-' + index + '.' + extension;
              files.push(new File([blob], name, { type: type }));
            }, function () {
              /* 忽略单个类型读取失败 */
            });
          });
        });
      });
      return chain.then(function () {
        if (files.length > 0) {
          handleFiles(files);
        }
      });
    }, function () {
      /* 用户拒绝剪贴板权限或 API 不可用时静默降级 */
    });
  }

  function handleFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (files.length === 0) {
      return;
    }
    if (state.uploading) {
      toast('正在识别中，请稍后再上传', 'error');
      return;
    }
    var images = files.filter(isSupportedImage);
    var skipped = files.length - images.length;
    if (images.length === 0) {
      toast('仅支持 PNG、JPEG、WEBP、GIF、BMP 图片', 'error');
      return;
    }
    if (skipped > 0) {
      toast('已忽略 ' + skipped + ' 个非图片文件');
    }
    uploadSequentially(images);
  }

  function uploadSequentially(files) {
    state.uploading = true;
    setUploadingUI(true);
    var succeeded = 0;

    return files.reduce(function (chain, file, index) {
      return chain.then(function () {
        setUploadStatus('识别中…（' + (index + 1) + ' / ' + files.length + '）');
        if (typeof file.size === 'number' && file.size > MAX_FILE_BYTES) {
          toast('图片「' + (file.name || '剪贴板截图') + '」超过 20MB 限制', 'error');
          return null;
        }
        return readFileAsDataURL(file).then(function (dataUrl) {
          return createNote(dataUrl);
        }).then(function () {
          succeeded += 1;
        }, function (error) {
          toast(error && error.message ? error.message : '上传失败', 'error');
        });
      });
    }, Promise.resolve()).then(function () {
      state.uploading = false;
      setUploadingUI(false);
      if (succeeded > 0) {
        setUploadStatus('已识别 ' + succeeded + ' 张，正在刷新列表…');
        return loadNotes().then(function () {
          clearUploadStatusLater(2200);
          toast('已添加 ' + succeeded + ' 条笔记', 'success');
        });
      }
      setUploadStatus('');
      return null;
    });
  }

  function setUploadingUI(uploading) {
    dom.dropzone.classList.toggle('is-uploading', uploading);
    dom.dropzone.setAttribute('aria-disabled', uploading ? 'true' : 'false');
    dom.fileInput.disabled = uploading;
    if (uploading) {
      setUploadStatus('识别中…');
    }
  }

  function setUploadStatus(text) {
    if (state.uploadStatusTimer) {
      window.clearTimeout(state.uploadStatusTimer);
      state.uploadStatusTimer = 0;
    }
    dom.uploadStatus.textContent = text;
  }

  function clearUploadStatusLater(delay) {
    if (state.uploadStatusTimer) {
      window.clearTimeout(state.uploadStatusTimer);
    }
    state.uploadStatusTimer = window.setTimeout(function () {
      dom.uploadStatus.textContent = '';
      state.uploadStatusTimer = 0;
    }, delay || 2000);
  }

  /* ---------- 详情 / 编辑 ---------- */

  function openDetail(noteId) {
    var note = findNote(noteId);
    if (!note) {
      toast('笔记不存在或已被删除', 'error');
      loadNotes();
      return;
    }
    state.currentNote = note;
    state.lastFocused = document.activeElement;
    fillDetail(note);
    dom.overlay.hidden = false;
    document.body.classList.add('modal-open');
    dom.detailTitle.focus();
  }

  function fillDetail(note) {
    dom.detailImage.hidden = false;
    dom.detailImageError.hidden = true;
    dom.detailImage.src = note.image_url || '';
    dom.detailImage.alt = '笔记截图原图';
    dom.detailTitle.value = typeof note.title === 'string' ? note.title : '';
    dom.detailContent.value = typeof note.content === 'string' ? note.content : '';
    dom.detailTime.textContent = note.created_at ? '创建于 ' + formatDateTime(note.created_at) : '';
    dom.detailTime.dateTime = note.created_at || '';
    dom.detailSave.disabled = false;
    dom.detailSave.textContent = '保存';
    dom.detailDelete.disabled = false;
    dom.detailDelete.textContent = '删除';
  }

  function onDetailImageError() {
    dom.detailImage.hidden = true;
    dom.detailImageError.hidden = false;
  }

  function closeDetail() {
    if (dom.overlay.hidden) {
      return;
    }
    dom.overlay.hidden = true;
    document.body.classList.remove('modal-open');
    state.currentNote = null;
    var previous = state.lastFocused;
    state.lastFocused = null;
    if (previous && previous.isConnected && typeof previous.focus === 'function') {
      previous.focus();
    } else if (dom.dropzone) {
      dom.dropzone.focus();
    }
  }

  function saveDetail() {
    var note = state.currentNote;
    if (!note || dom.detailSave.disabled) {
      return;
    }
    var payload = {
      title: dom.detailTitle.value.trim(),
      content: dom.detailContent.value
    };
    dom.detailSave.disabled = true;
    dom.detailSave.textContent = '保存中…';
    dom.detailDelete.disabled = true;

    requestJSON('/api/notes/' + encodeURIComponent(note.id), jsonRequestOptions('PUT', payload)).then(function (data) {
      var updated = data && data.note ? data.note : null;
      if (!updated) {
        updated = Object.assign({}, note, payload);
      }
      upsertNote(updated);
      renderNotes();
      if (!dom.overlay.hidden && state.currentNote && String(state.currentNote.id) === String(note.id)) {
        state.currentNote = updated;
        fillDetail(updated);
      }
      toast('已保存', 'success');
    }, function (error) {
      toast(error && error.message ? error.message : '保存失败', 'error');
    }).then(function () {
      if (dom.detailSave.textContent === '保存中…') {
        dom.detailSave.disabled = false;
        dom.detailSave.textContent = '保存';
      }
      dom.detailDelete.disabled = false;
    });
  }

  function deleteDetail() {
    var note = state.currentNote;
    if (!note || dom.detailDelete.disabled) {
      return;
    }
    if (!window.confirm('确定删除这条笔记？删除后不可恢复。')) {
      return;
    }
    dom.detailDelete.disabled = true;
    dom.detailDelete.textContent = '删除中…';
    dom.detailSave.disabled = true;

    requestJSON('/api/notes/' + encodeURIComponent(note.id), { method: 'DELETE' }).then(function () {
      state.notes = state.notes.filter(function (item) {
        return String(item.id) !== String(note.id);
      });
      closeDetail();
      renderNotes();
      toast('已删除', 'success');
    }, function (error) {
      toast(error && error.message ? error.message : '删除失败', 'error');
      dom.detailDelete.disabled = false;
      dom.detailDelete.textContent = '删除';
      dom.detailSave.disabled = false;
    });
  }

  function onDocumentKeydown(event) {
    if (event.key === 'Escape' && !dom.overlay.hidden) {
      event.preventDefault();
      closeDetail();
    }
  }

  /* ---------- toast ---------- */

  function toast(message, type) {
    if (!dom.toastContainer || !message) {
      return;
    }
    var element = document.createElement('div');
    element.className = 'toast' + (type === 'error' ? ' toast-error' : '');
    element.setAttribute('role', type === 'error' ? 'alert' : 'status');
    element.textContent = message;
    dom.toastContainer.appendChild(element);
    window.setTimeout(function () {
      if (element.parentNode) {
        element.parentNode.removeChild(element);
      }
    }, type === 'error' ? 4200 : 2600);
  }
})();
