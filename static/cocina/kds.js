(function () {
  'use strict';
  let comandas = [], currentPage = 0, totalPages = 1, pollTimer = null, rotateTimer = null, clockTimer = null, timerInterval = null;
  let ws = null, wsReconnectAttempts = 0, wsMaxReconnect = 10;
  const pendingActions = new Map(), disappearingCards = new Map(), cancelledLines = new Map(), alreadyAlerted = new Set();
  let modalLineaId = null, modalCantidadOriginal = 1;

  const grid = document.getElementById('kds-grid'), loading = document.getElementById('kds-loading'),
    filtroZona = document.getElementById('filtro-zona'), totalPedidosEl = document.getElementById('total-pedidos'),
    pedidosUrgentesEl = document.getElementById('pedidos-urgentes'), paginaActualEl = document.getElementById('pagina-actual'),
    totalPaginasEl = document.getElementById('total-paginas'), paginationEl = document.getElementById('kds-pagination'),
    paginationDots = document.getElementById('pagination-dots'), autoRotateCheck = document.getElementById('auto-rotate'),
    clockTimeEl = document.getElementById('clock-time'), clockDateEl = document.getElementById('clock-date'),
    toastEl = document.getElementById('kds-toast'), wsIndicator = document.getElementById('ws-indicator'),
    modalOverlay = document.getElementById('modal-anulacion'), modalPlatoNombre = document.getElementById('modal-plato-nombre'),
    modalMotivo = document.getElementById('modal-motivo'), modalError = document.getElementById('modal-error'),
    modalChars = document.getElementById('modal-chars'),
    modalParcialWrapper = document.getElementById('modal-parcial-wrapper'),
    modalCantidadParcial = document.getElementById('modal-cantidad-parcial');

  const UNDO_SECONDS = 10, DISAPPEAR_SECONDS = 5, CANCELLED_VISIBLE_SECONDS = 30;
  const ACTION_LABELS = { 'EN_PREP': 'Recibir', 'LISTO': 'Servir', 'ANULADO': 'Cancelar' };

  // ========= CONSTANTES DE TIEMPO — REGLA 2 =========
  // Amarillo: >= 10 min desde llegada de la comanda
  // Rojo/Urgente: >= 15 min desde llegada de la comanda
  const MINS_ALERTA = 10, MINS_URGENTE = 15;

  let audioCtx = null;
  function playAlertBeep() {
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.type = 'square'; o.frequency.setValueAtTime(880, audioCtx.currentTime);
      g.gain.setValueAtTime(0.3, audioCtx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.5);
      o.start(audioCtx.currentTime); o.stop(audioCtx.currentTime + 0.5);
      setTimeout(() => {
        const o2 = audioCtx.createOscillator(), g2 = audioCtx.createGain();
        o2.connect(g2); g2.connect(audioCtx.destination);
        o2.type = 'square'; o2.frequency.setValueAtTime(1100, audioCtx.currentTime);
        g2.gain.setValueAtTime(0.3, audioCtx.currentTime);
        g2.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.4);
        o2.start(audioCtx.currentTime); o2.stop(audioCtx.currentTime + 0.4);
      }, 300);
    } catch (e) { console.warn('Audio alert failed', e); }
  }

  function init() { startClock(); startLiveTimers(); connectWebSocket(); startPolling(); fetchComandas(); fetchResumen(); setupEventListeners(); }

  function setupEventListeners() {
    if (filtroZona) filtroZona.addEventListener('change', () => { currentPage = 0; fetchComandas(); });
    if (autoRotateCheck) autoRotateCheck.addEventListener('change', () => { autoRotateCheck.checked ? startRotation() : stopRotation(); });
    if (modalMotivo) modalMotivo.addEventListener('input', () => { modalChars.textContent = modalMotivo.value.length; if (modalMotivo.value.trim()) modalError.style.display = 'none'; });
  }

  // ========= WEBSOCKET =========
  function connectWebSocket() {
    if (!KDS_CONFIG.wsUrl) return startPolling();
    try {
      ws = new WebSocket(KDS_CONFIG.wsUrl);
      ws.onopen = () => { wsReconnectAttempts = 0; setWsStatus(true); };
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data); if (d.type === 'kds_update') { fetchComandas(); fetchResumen(); }
          else if (d.type === 'pong') { }
        } catch (err) { console.error('WS parse error', err); }
      };
      ws.onclose = () => { setWsStatus(false); scheduleReconnect(); };
      ws.onerror = () => { ws.close(); };
    } catch (e) { console.error('WS connect error', e); startPolling(); }
  }
  function scheduleReconnect() {
    if (wsReconnectAttempts >= wsMaxReconnect) { startPolling(); return; }
    const delay = Math.min(1000 * Math.pow(2, wsReconnectAttempts), 30000);
    wsReconnectAttempts++;
    setTimeout(connectWebSocket, delay);
    if (!pollTimer) startPolling();
  }
  function setWsStatus(connected) {
    if (wsIndicator) { wsIndicator.classList.toggle('kds-ws-indicator--on', connected); wsIndicator.classList.toggle('kds-ws-indicator--off', !connected); wsIndicator.title = connected ? 'WebSocket conectado' : 'WebSocket desconectado'; }
  }
  function startPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = setInterval(() => { fetchComandas(); fetchResumen(); }, KDS_CONFIG.pollInterval); }

  // ========= FETCH =========
  async function fetchComandas() {
    try {
      const z = filtroZona ? filtroZona.value : '';
      let url = `${KDS_CONFIG.apiBaseUrl}/comandas-activas/`;
      if (z) url += `?zona=${encodeURIComponent(z)}`;
      const r = await fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' });
      if (!r.ok) {
        throw new Error(`HTTP ${r.status}`);
      }
      const data = await r.json();
      if (!Array.isArray(data)) {
        throw new Error('Respuesta inválida del servidor');
      }
      const prevIds = new Set(comandas.map(c => c.id));
      const newOrders = data.filter(c => !prevIds.has(c.id));
      if (newOrders.length > 0 && comandas.length > 0) showToast(`🔔 ${newOrders.length} nuevo(s) pedido(s)`, 'warning');

      // Ordenar por prioridad (urgente > alerta > normal) y luego por antigüedad
      data.sort((a, b) => {
        const tA = new Date(a.fecha_apertura).getTime();
        const tB = new Date(b.fecha_apertura).getTime();
        const minsA = Math.floor((Date.now() - tA) / 60000);
        const minsB = Math.floor((Date.now() - tB) / 60000);

        let priorityA = 0;
        if (minsA >= MINS_URGENTE) priorityA = 2;
        else if (minsA >= MINS_ALERTA) priorityA = 1;

        let priorityB = 0;
        if (minsB >= MINS_URGENTE) priorityB = 2;
        else if (minsB >= MINS_ALERTA) priorityB = 1;

        if (priorityA !== priorityB) {
          return priorityB - priorityA; // Mayor prioridad primero
        }
        return tA - tB; // Mismo nivel: más antiguo primero
      });

      comandas = data; renderComandas(); fetchResumen();
    } catch (err) {
      console.error('Error fetching comandas:', err);
      comandas = [];
      if (grid) {
        grid.innerHTML = '<div class="kds-empty"><div class="kds-empty__icon"></div><div class="kds-empty__text">No se pudieron cargar las comandas</div><div class="kds-empty__subtext">Verifique su sesión (rol Cocinero/Admin) y recargue la página</div></div>';
      }
      if (typeof showToast === 'function') showToast('Error al cargar comandas del KDS', 'error');
    } finally {
      if (loading) loading.style.display = 'none';
    }
  }
  async function fetchResumen() {
    try {
      const z = filtroZona.value; let url = `${KDS_CONFIG.apiBaseUrl}/resumen/`; if (z) url += `?zona=${z}`;
      const r = await fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      totalPedidosEl.textContent = d.total_pedidos;
      pedidosUrgentesEl.textContent = d.pedidos_urgentes;
      const recienLlegadosEl = document.getElementById('pedidos-recien-llegados');
      if (recienLlegadosEl) recienLlegadosEl.textContent = d.recien_llegados || 0;
    } catch (err) { console.error('Error fetching resumen:', err); }
  }

  // ========= LIVE TIMERS =========
  function startLiveTimers() { if (timerInterval) clearInterval(timerInterval); timerInterval = setInterval(updateLiveTimers, 1000); }

  function updateLiveTimers() {
    let urgentCount = 0;

    // ── REGLA 2: Contador a nivel de COMANDA (tarjeta completa) ──
    document.querySelectorAll('.kds-card[data-comanda-apertura]').forEach(cardEl => {
      const apertura = new Date(cardEl.dataset.comandaApertura);
      const diffMs = Date.now() - apertura.getTime();
      const mins = Math.floor(diffMs / 60000);
      const secs = Math.floor((diffMs % 60000) / 1000);

      // Actualizar el timer visual del header de la tarjeta
      const timerEl = cardEl.querySelector('.kds-card__timer');
      if (timerEl) {
        timerEl.querySelector('.kds-card__timer-value').textContent = `${mins}m ${secs < 10 ? '0' : ''}${secs}s`;
        timerEl.classList.remove('kds-card__timer--alerta', 'kds-card__timer--urgente');
        if (mins >= MINS_URGENTE) {
          timerEl.classList.add('kds-card__timer--urgente');
        } else if (mins >= MINS_ALERTA) {
          timerEl.classList.add('kds-card__timer--alerta');
        }
      }

      // Cambiar color de la tarjeta completa basado en el timer de comanda
      cardEl.classList.remove('kds-card--normal', 'kds-card--alerta', 'kds-card--urgente');
      if (mins >= MINS_URGENTE) {
        cardEl.classList.add('kds-card--urgente');
        urgentCount++;
        const cid = parseInt(cardEl.dataset.comandaId);
        if (!alreadyAlerted.has(cid)) {
          alreadyAlerted.add(cid);
          playAlertBeep();
        }
      } else if (mins >= MINS_ALERTA) {
        cardEl.classList.add('kds-card--alerta');
      } else {
        cardEl.classList.add('kds-card--normal');
      }
    });

    // ── REGLA 1: Contador individual por PLATO (solo EN_PREP) ──
    document.querySelectorAll('.kds-linea[data-prep-start]').forEach(el => {
      const start = new Date(el.dataset.prepStart);
      const diffMs = Date.now() - start.getTime();
      const mins = Math.floor(diffMs / 60000);
      const secs = Math.floor((diffMs % 60000) / 1000);
      const totalSecs = Math.floor(diffMs / 1000);
      const tiempoEstimado = parseInt(el.dataset.tiempoEstimado) || 0;
      const estimadoSecs = tiempoEstimado * 60;

      const timeEl = el.querySelector('.kds-linea__tiempo');
      if (timeEl) {
        timeEl.textContent = `${mins}m ${secs < 10 ? '0' : ''}${secs}s`;
        // Comparar contra el tiempo estimado del plato
        timeEl.classList.remove('kds-linea__tiempo--alerta', 'kds-linea__tiempo--urgente', 'kds-linea__tiempo--excedido', 'kds-linea__tiempo--critico');
        if (tiempoEstimado > 0) {
          if (totalSecs > estimadoSecs * 1.5) {
            // Critico: supera 150% del estimado
            timeEl.classList.add('kds-linea__tiempo--critico');
          } else if (totalSecs > estimadoSecs) {
            // Excedido: supera el estimado
            timeEl.classList.add('kds-linea__tiempo--excedido');
          }
        }
      }

      // Actualizar barra de progreso del plato
      const fillEl = el.querySelector('.kds-linea__timer-fill');
      if (fillEl && tiempoEstimado > 0) {
        const pct = Math.min((totalSecs / estimadoSecs) * 100, 100);
        fillEl.style.width = `${pct}%`;
        fillEl.classList.remove('kds-linea__timer-fill--warning', 'kds-linea__timer-fill--danger');
        if (totalSecs > estimadoSecs) {
          fillEl.style.width = '100%';
          fillEl.classList.add('kds-linea__timer-fill--danger');
        } else if (pct >= 75) {
          fillEl.classList.add('kds-linea__timer-fill--warning');
        }
      }
    });

    // Actualizar resumen de urgentes
    if (pedidosUrgentesEl) {
      pedidosUrgentesEl.textContent = urgentCount;
    }
  }

  // ========= RENDER =========
  function renderComandas() {
    const ipp = KDS_CONFIG.itemsPerPage; totalPages = Math.max(1, Math.ceil(comandas.length / ipp));
    if (currentPage >= totalPages) currentPage = 0;
    const start = currentPage * ipp, pageComandas = comandas.slice(start, start + ipp);
    grid.innerHTML = '';
    if (comandas.length === 0) {
      grid.innerHTML = '<div class="kds-empty"><div class="kds-empty__icon"></div><div class="kds-empty__text">Sin pedidos pendientes</div><div class="kds-empty__subtext">Las nuevas comandas aparecerán automáticamente</div></div>';
      paginationEl.style.display = 'none'; stopRotation(); return;
    }
    pageComandas.forEach(c => { const card = createCard(c); grid.appendChild(card); checkCardCompletion(c, card); });
    updatePagination();
    if (totalPages > 1 && autoRotateCheck.checked) startRotation(); else stopRotation();
  }

  function createCard(comanda) {
    const card = document.createElement('article');
    card.className = 'kds-card kds-card--normal';
    card.dataset.comandaId = comanda.id;
    // REGLA 2: data attr para el timer de comanda
    card.dataset.comandaApertura = comanda.fecha_apertura;

    const fecha = new Date(comanda.fecha_apertura); const fechaStr = formatDateTime(fecha);
    const notas = buildNotas(comanda);

    // Calcular timer de comanda para el render inicial
    const diffMsComanda = Date.now() - fecha.getTime();
    const minsComanda = Math.floor(diffMsComanda / 60000);
    const secsComanda = Math.floor((diffMsComanda % 60000) / 1000);
    let timerClass = '';
    if (minsComanda >= MINS_URGENTE) {
      timerClass = 'kds-card__timer--urgente';
      card.className = 'kds-card kds-card--urgente';
      if (!alreadyAlerted.has(comanda.id)) { alreadyAlerted.add(comanda.id); playAlertBeep(); }
    } else if (minsComanda >= MINS_ALERTA) {
      timerClass = 'kds-card__timer--alerta';
      card.className = 'kds-card kds-card--alerta';
    }

    // Build lines including cancelled-visible ones
    let lineasHTML = comanda.lineas.map(l => createLineaHTML(l, comanda)).join('');
    // Add cancelled lines still visible (30s)
    cancelledLines.forEach((cl, lid) => {
      if (cl.comandaId === comanda.id) {
        lineasHTML += `<div class="kds-linea kds-linea--anulada" data-linea-id="${lid}">
        <span class="kds-linea__nombre">${cl.platoNombre}</span>
        <span class="kds-linea__cantidad">x${cl.cantidad}</span>
        <span class="kds-linea__estado kds-linea__estado--ANULADO">ANULADO</span>
        <span class="kds-linea__tiempo">${cl.segundosRestantes}s</span>
      </div>`;
      }
    });

    card.innerHTML = `<div class="kds-card__header">
      <span class="kds-card__pedido">Pedido #${comanda.numero_pedido}</span>
      <div class="kds-card__header-right">
        <span class="kds-card__timer ${timerClass}"><span class="kds-card__timer-icon">⏱</span><span class="kds-card__timer-value">${minsComanda}m ${secsComanda < 10 ? '0' : ''}${secsComanda}s</span></span>
        <span class="kds-card__fecha">${fechaStr}</span>
      </div>
    </div>
    <div class="kds-card__subheader"><span class="kds-card__codigo">${comanda.codigo_comanda}</span><span class="kds-card__info">Mesa: <strong>${comanda.mesa_numero}</strong> — Piso: <strong>${comanda.zona_nombre}</strong></span>${comanda.nombre_cliente ? `<span class="kds-card__cliente">Cliente: <strong>${escapeHtml(comanda.nombre_cliente)}</strong></span>` : ''}<span class="kds-card__mesero">Mesero: ${comanda.mozo_nombre}</span></div>
    <div class="kds-card__body"><hr class="kds-card__divider">${lineasHTML}</div>${notas}`;

    comanda.lineas.forEach(l => { if (pendingActions.has(l.id)) { const p = pendingActions.get(l.id); const el = card.querySelector(`[data-linea-id="${l.id}"]`); if (el) applyPendingVisual(el, p); } });
    return card;
  }

  function createLineaHTML(linea, comanda) {
    const hasPending = pendingActions.has(linea.id);
    const buttons = hasPending ? '' : getContextualButtons(linea);
    const isPrep = linea.estado === 'EN_PREP' && linea.fecha_inicio_prep_iso;
    const tiempoEstimado = linea.tiempo_estimado || 0;

    // REGLA 1: Timer individual SOLO arranca al Recibir (EN_PREP)
    // En PENDIENTE: mostrar "—" (no ha sido recibido)
    const prepAttr = isPrep ? ` data-prep-start="${linea.fecha_inicio_prep_iso}" data-tiempo-estimado="${tiempoEstimado}"` : '';

    let tiempoDisplay = '—';
    let tiempoClass = '';
    let estimadoHTML = '';
    let timerBarHTML = '';

    if (isPrep) {
      const d = Date.now() - new Date(linea.fecha_inicio_prep_iso).getTime();
      const m = Math.floor(d / 60000), s = Math.floor((d % 60000) / 1000);
      const totalSecs = Math.floor(d / 1000);
      const estimadoSecs = tiempoEstimado * 60;
      tiempoDisplay = `${m}m ${s < 10 ? '0' : ''}${s}s`;

      // Comparar contra estimado
      if (tiempoEstimado > 0) {
        estimadoHTML = `<span class="kds-linea__estimado">Est: ${tiempoEstimado}m</span>`;
        if (totalSecs > estimadoSecs * 1.5) {
          tiempoClass = 'kds-linea__tiempo--critico';
        } else if (totalSecs > estimadoSecs) {
          tiempoClass = 'kds-linea__tiempo--excedido';
        }
        // Barra de progreso
        const pct = Math.min((totalSecs / estimadoSecs) * 100, 100);
        let fillClass = '';
        if (totalSecs > estimadoSecs) fillClass = 'kds-linea__timer-fill--danger';
        else if (pct >= 75) fillClass = 'kds-linea__timer-fill--warning';
        timerBarHTML = `<div class="kds-linea__timer-bar"><div class="kds-linea__timer-fill ${fillClass}" style="width:${totalSecs > estimadoSecs ? 100 : pct}%"></div></div>`;
      }
    } else if (linea.estado === 'LISTO' && linea.tiempo_real_preparacion_seg) {
      // Mostrar tiempo real de coccion para platos ya listos (auditoria)
      const m = Math.floor(linea.tiempo_real_preparacion_seg / 60);
      const s = linea.tiempo_real_preparacion_seg % 60;
      tiempoDisplay = `${m}m ${s < 10 ? '0' : ''}${s}s`;
      if (tiempoEstimado > 0) {
        estimadoHTML = `<span class="kds-linea__estimado">Est: ${tiempoEstimado}m</span>`;
      }
    }

    return `<div class="kds-linea" data-linea-id="${linea.id}"${prepAttr}>
    <span class="kds-linea__nombre">${linea.plato_nombre}</span>
    <span class="kds-linea__cantidad">x${linea.cantidad}</span>
    <span class="kds-linea__orden">${linea.orden_entrega}</span>
    <span class="kds-linea__estado kds-linea__estado--${linea.estado}">${linea.estado_display}</span>
    <span class="kds-linea__timer-group">
      <span class="kds-linea__tiempo ${tiempoClass}">${tiempoDisplay}</span>
      ${estimadoHTML}
      ${timerBarHTML}
    </span>
    <div class="kds-linea__actions">${buttons}</div></div>`;
  }

  function applyPendingVisual(lineaEl, pending) {
    const a = lineaEl.querySelector('.kds-linea__actions'); if (!a) return;
    const label = ACTION_LABELS[pending.nuevoEstado] || pending.nuevoEstado;
    const pct = ((UNDO_SECONDS - pending.segundosRestantes) / UNDO_SECONDS) * 100;
    a.innerHTML = `<div class="kds-undo" data-linea-id="${lineaEl.dataset.lineaId}"><div class="kds-undo__info"><span class="kds-undo__label">${label} en <strong class="kds-undo__seconds">${pending.segundosRestantes}s</strong></span><button class="kds-undo__btn" onclick="KDS.deshacerAccion(${lineaEl.dataset.lineaId})">Deshacer</button></div><div class="kds-undo__bar"><div class="kds-undo__progress kds-undo__progress--${pending.nuevoEstado}" style="width:${pct}%"></div></div></div>`;
    lineaEl.classList.add('kds-linea--pending');
  }

  function getContextualButtons(l) {
    switch (l.estado) {
      case 'PENDIENTE': return `<button class="kds-btn kds-btn--servir" onclick="KDS.cambiarEstado(${l.id},'LISTO')">Servir</button>`;
      case 'EN_PREP': return `<button class="kds-btn kds-btn--servir" onclick="KDS.cambiarEstado(${l.id},'LISTO')">Servir</button>`;
      case 'LISTO': return '';
      default: return '';
    }
  }

  function escapeHtml(str) {
    return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
  }

  function getCardUrgencyClass(c) {
    // REGLA 2: Basado en tiempo desde la apertura de la comanda
    const aperturaMs = c.fecha_apertura ? new Date(c.fecha_apertura).getTime() : 0;
    if (aperturaMs <= 0) return 'kds-card--normal';
    const mins = Math.floor((Date.now() - aperturaMs) / 60000);
    if (mins >= MINS_URGENTE) return 'kds-card--urgente';
    if (mins >= MINS_ALERTA) return 'kds-card--alerta';
    return 'kds-card--normal';
  }

  function buildNotas(c) {
    const n = []; c.lineas.forEach(l => { if (l.observacion) n.push(`${l.plato_nombre}: ${l.observacion}`); });
    if (c.observacion_general) n.push(c.observacion_general); if (!n.length) return '';
    return `<div class="kds-card__nota"><div class="kds-card__nota-title">Nota:</div>${n.join('<br>')}</div>`;
  }

  // ========= MODAL ANULACIÓN — REGLA 3 =========
  function abrirModalAnulacion(lineaId, platoNombre, cantidad) {
    modalLineaId = lineaId;
    modalCantidadOriginal = cantidad || 1;
    modalPlatoNombre.textContent = `${platoNombre} (x${cantidad})`;
    modalMotivo.value = ''; modalChars.textContent = '0'; modalError.style.display = 'none';

    // Llenar el select de cantidad parcial dinámicamente — SIEMPRE visible
    if (modalCantidadParcial) {
      modalCantidadParcial.innerHTML = '';

      // Opción por defecto: cancelar todos, no cocinar ninguno
      const opt0 = document.createElement('option');
      opt0.value = '0';
      opt0.textContent = cantidad === 1
        ? `Cancelar todo (1 unidad) — No cocinar ninguno`
        : `Cancelar todos (${cantidad} uds.) — No cocinar ninguno`;
      modalCantidadParcial.appendChild(opt0);

      // Opciones intermedias: cocinar parcialmente (solo si cantidad > 1)
      for (let cocinar = 1; cocinar < cantidad; cocinar++) {
        const cancelar = cantidad - cocinar;
        const opt = document.createElement('option');
        opt.value = String(cocinar);
        opt.textContent = `Cocinar ${cocinar} — Cancelar ${cancelar} de ${cantidad}`;
        modalCantidadParcial.appendChild(opt);
      }

      modalCantidadParcial.value = '0';

      // Listener para actualizar el resumen dinámico
      modalCantidadParcial.onchange = () => {
        updateParcialSummary(cantidad);
      };
    }

    // Siempre mostrar el bloque de cantidad parcial
    if (modalParcialWrapper) {
      modalParcialWrapper.style.display = 'block';
    }

    // Actualizar el resumen inicial
    updateParcialSummary(cantidad);

    modalOverlay.style.display = 'flex'; modalMotivo.focus();
  }

  function updateParcialSummary(cantidadTotal) {
    const infoEl = document.querySelector('.kds-modal__parcial-info');
    if (!infoEl || !modalCantidadParcial) return;
    const cocinar = parseInt(modalCantidadParcial.value) || 0;
    const cancelar = cantidadTotal - cocinar;
    if (cocinar === 0) {
      infoEl.textContent = `Se cancelarán las ${cantidadTotal} unidad(es) completas. No se cocinará ninguna.`;
    } else {
      infoEl.innerHTML = `<strong>Se cancelarán ${cancelar}</strong> unidad(es) y <strong>se cocinarán ${cocinar}</strong>. Esta información se notificará al mesero.`;
    }
  }

  function cerrarModalAnulacion() { modalOverlay.style.display = 'none'; modalLineaId = null; modalCantidadOriginal = 1; }

  function confirmarAnulacion() {
    const motivo = modalMotivo.value.trim();
    if (!motivo) { modalError.style.display = 'block'; modalMotivo.focus(); return; }
    const lineaId = modalLineaId;
    const cantidadParcial = modalCantidadParcial ? parseInt(modalCantidadParcial.value) || 0 : 0;
    cerrarModalAnulacion();
    cambiarEstado(lineaId, 'ANULADO', motivo, cantidadParcial);
  }

  // ========= UNDO SYSTEM =========
  function cambiarEstado(lineaId, nuevoEstado, motivo = '', cantidadParcial = 0) {
    if (pendingActions.has(lineaId)) return;

    // ANULADO es inmediato: ya fue confirmado en el modal con motivo
    if (nuevoEstado === 'ANULADO') {
      let platoNombre = '', cantidad = 1, comandaId = null, estadoOrig = null;
      for (const c of comandas) { for (const l of c.lineas) { if (l.id === lineaId) { estadoOrig = l.estado; platoNombre = l.plato_nombre; cantidad = l.cantidad; comandaId = c.id; break; } } if (estadoOrig) break; }
      if (!estadoOrig) return;
      confirmarAccionInmediata(lineaId, nuevoEstado, motivo, platoNombre, cantidad, comandaId, cantidadParcial);
      return;
    }

    let estadoOrig = null, platoNombre = '', cantidad = 1, comandaId = null;
    for (const c of comandas) { for (const l of c.lineas) { if (l.id === lineaId) { estadoOrig = l.estado; platoNombre = l.plato_nombre; cantidad = l.cantidad; comandaId = c.id; break; } } if (estadoOrig) break; }
    if (!estadoOrig) return;
    const pending = { nuevoEstado, estadoOriginal: estadoOrig, segundosRestantes: UNDO_SECONDS, intervalId: null, motivo, platoNombre, cantidad, comandaId };
    pendingActions.set(lineaId, pending);
    const el = document.querySelector(`[data-linea-id="${lineaId}"]`); if (el) applyPendingVisual(el, pending);
    const label = ACTION_LABELS[nuevoEstado] || nuevoEstado;
    showToast(`${label} en ${UNDO_SECONDS}s — puedes deshacer`, 'warning');
    pending.intervalId = setInterval(() => {
      pending.segundosRestantes--; updateUndoVisual(lineaId, pending);
      if (pending.segundosRestantes <= 0) { clearInterval(pending.intervalId); confirmarAccion(lineaId); }
    }, 1000);
  }
  function updateUndoVisual(lid, p) {
    const u = document.querySelector(`.kds-undo[data-linea-id="${lid}"]`); if (!u) return;
    const s = u.querySelector('.kds-undo__seconds'), pr = u.querySelector('.kds-undo__progress');
    if (s) s.textContent = `${p.segundosRestantes}s`;
    if (pr) pr.style.width = `${((UNDO_SECONDS - p.segundosRestantes) / UNDO_SECONDS) * 100}%`;
  }
  function deshacerAccion(lid) {
    const p = pendingActions.get(lid); if (!p) return;
    if (p.intervalId) clearInterval(p.intervalId); pendingActions.delete(lid);
    showToast(' Acción deshecha', 'success'); renderComandas();
  }

  async function confirmarAccionInmediata(lid, nuevoEstado, motivo, platoNombre, cantidad, comandaId, cantidadParcial = 0) {
    showToast(' Cancelando plato...', 'warning');
    try {
      const body = { nuevo_estado: nuevoEstado };
      if (motivo) body.motivo = motivo;
      if (cantidadParcial > 0) body.cantidad_parcial = cantidadParcial;
      const r = await fetch(`${KDS_CONFIG.apiBaseUrl}/lineas/${lid}/cambiar-estado/`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': KDS_CONFIG.csrfToken, 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin', body: JSON.stringify(body) });
      const d = await r.json();
      if (r.ok) {
        showToast(` ${d.mensaje}`, 'success');
        startCancelledVisibility(lid, { platoNombre, cantidad, comandaId, motivo });
        await fetchComandas();
      } else { showToast(` ${d.error || 'Error al cancelar'}`, 'error'); renderComandas(); }
    } catch (err) { console.error('Error:', err); showToast(' Error de conexión', 'error'); renderComandas(); }
  }

  async function confirmarAccion(lid) {
    const p = pendingActions.get(lid); if (!p) return;
    if (p.intervalId) clearInterval(p.intervalId); pendingActions.delete(lid);
    try {
      const body = { nuevo_estado: p.nuevoEstado }; if (p.motivo) body.motivo = p.motivo;
      const r = await fetch(`${KDS_CONFIG.apiBaseUrl}/lineas/${lid}/cambiar-estado/`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': KDS_CONFIG.csrfToken, 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin', body: JSON.stringify(body) });
      const d = await r.json();
      if (r.ok) {
        showToast(` ${d.mensaje}`, 'success');
        if (p.nuevoEstado === 'ANULADO') startCancelledVisibility(lid, p);
        await fetchComandas();
      } else { showToast(` ${d.error}`, 'error'); renderComandas(); }
    } catch (err) { console.error('Error:', err); showToast(' Error de conexión', 'error'); renderComandas(); }
  }

  // ========= CANCELLED LINE 30s VISIBILITY =========
  function startCancelledVisibility(lid, p) {
    const entry = { platoNombre: p.platoNombre, cantidad: p.cantidad, comandaId: p.comandaId, segundosRestantes: CANCELLED_VISIBLE_SECONDS, intervalId: null };
    cancelledLines.set(lid, entry);
    entry.intervalId = setInterval(() => {
      entry.segundosRestantes--;
      if (entry.segundosRestantes <= 0) { clearInterval(entry.intervalId); cancelledLines.delete(lid); renderComandas(); }
    }, 1000);
  }

  // ========= CARD DISAPPEAR =========
  function checkCardCompletion(c, cardEl) {
    if (disappearingCards.has(c.id)) { const ex = disappearingCards.get(c.id); showDisappearOverlay(cardEl, ex.segundosRestantes); return; }
    const all = c.lineas.every(l => { if (pendingActions.has(l.id)) { const p = pendingActions.get(l.id); return ['LISTO', 'ANULADO', 'ENTREGADO'].includes(p.nuevoEstado); } return ['LISTO', 'ANULADO', 'ENTREGADO'].includes(l.estado); });
    if (all && c.lineas.length > 0) startDisappearCountdown(c.id, cardEl);
  }
  function startDisappearCountdown(cid, cardEl) {
    if (disappearingCards.has(cid)) return;
    const e = { segundosRestantes: DISAPPEAR_SECONDS, intervalId: null }; disappearingCards.set(cid, e);
    showDisappearOverlay(cardEl, e.segundosRestantes);
    e.intervalId = setInterval(() => {
      e.segundosRestantes--;
      const ov = cardEl.querySelector('.kds-disappear');
      if (ov) {
        const ct = ov.querySelector('.kds-disappear__count'); const ci = ov.querySelector('.kds-disappear__circle-progress');
        if (ct) ct.textContent = e.segundosRestantes;
        if (ci) { const circ = 2 * Math.PI * 22; ci.style.strokeDashoffset = circ * (1 - e.segundosRestantes / DISAPPEAR_SECONDS); }
      }
      if (e.segundosRestantes <= 0) { clearInterval(e.intervalId); disappearingCards.delete(cid); cardEl.classList.add('kds-card--fade-out'); setTimeout(() => { comandas = comandas.filter(c => c.id !== cid); renderComandas(); }, 500); }
    }, 1000);
  }
  function showDisappearOverlay(cardEl, s) {
    if (cardEl.querySelector('.kds-disappear')) return; cardEl.classList.add('kds-card--completing');
    const circ = 2 * Math.PI * 22, off = circ * (1 - s / DISAPPEAR_SECONDS);
    const ov = document.createElement('div'); ov.className = 'kds-disappear';
    ov.innerHTML = `<div class="kds-disappear__content"><svg class="kds-disappear__svg" viewBox="0 0 50 50"><circle class="kds-disappear__circle-bg" cx="25" cy="25" r="22"/><circle class="kds-disappear__circle-progress" cx="25" cy="25" r="22" style="stroke-dasharray:${circ};stroke-dashoffset:${off}"/></svg><span class="kds-disappear__count">${s}</span></div><span class="kds-disappear__label">Pedido completado</span>`;
    cardEl.appendChild(ov);
  }

  // ========= PAGINATION =========
  function updatePagination() {
    if (totalPages <= 1) { paginationEl.style.display = 'none'; return; }
    paginationEl.style.display = 'block'; paginaActualEl.textContent = currentPage + 1; totalPaginasEl.textContent = totalPages;
    paginationDots.innerHTML = '';
    for (let i = 0; i < totalPages; i++) { const d = document.createElement('div'); d.className = `kds-pagination__dot${i === currentPage ? ' kds-pagination__dot--active' : ''}`; d.addEventListener('click', () => { currentPage = i; renderComandas(); if (autoRotateCheck.checked) startRotation(); }); paginationDots.appendChild(d); }
  }
  function startRotation() { stopRotation(); if (totalPages <= 1) return; rotateTimer = setInterval(() => { currentPage = (currentPage + 1) % totalPages; renderComandas(); }, KDS_CONFIG.rotateInterval); }
  function stopRotation() { if (rotateTimer) { clearInterval(rotateTimer); rotateTimer = null; } }

  // ========= CLOCK =========
  function startClock() {
    if (!clockTimeEl || !clockDateEl) return;
    updateClock();
    clockTimer = setInterval(updateClock, 1000);
  }
  function updateClock() {
    if (!clockTimeEl || !clockDateEl) return;
    const n = new Date();
    clockTimeEl.textContent = n.toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    clockDateEl.textContent = n.toLocaleDateString('es-PE', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  }

  // ========= TOAST =========
  function showToast(msg, type = 'success') { toastEl.textContent = msg; toastEl.className = `kds-toast kds-toast--${type} kds-toast--show`; setTimeout(() => { toastEl.className = 'kds-toast'; }, 3000); }

  function formatDateTime(d) { return d.toLocaleString('es-PE', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }); }

  window.KDS = { cambiarEstado, deshacerAccion, abrirModalAnulacion, cerrarModalAnulacion, confirmarAnulacion };
  document.addEventListener('DOMContentLoaded', init);
})();
