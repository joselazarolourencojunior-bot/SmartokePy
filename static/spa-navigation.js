/**
 * SPA (Single Page Application) Navigation System for Pikaraoke
 * Enables dynamic content loading without full page refreshes
 */

(function() {
    'use strict';

    // Configuration
    const config = {
        contentSelector: '.box',
        linkSelector: 'a[href]', // Intercept all links, not just navbar
        notificationSelector: '#notification-alt',
        scrollBehavior: 'smooth'
    };

    // State management
    let isNavigating = false;
    let currentPath = window.location.pathname + window.location.search;
    let loadedResources = new Set(); // Track loaded external resources
    let isInitialized = false; // Prevent multiple initializations

    /**
     * Initialize SPA navigation
     */
    function init() {
        // Prevent double initialization
        if (isInitialized) {
            console.log('SPA Navigation already initialized, skipping');
            return;
        }

        // Handle navigation clicks
        attachNavListeners();

        // Handle browser back/forward buttons
        window.addEventListener('popstate', handlePopState);

        // Ensure hamburger menu works across all pages
        initHamburgerMenu();

        // Initialize username change handler
        initUsernameHandler();

        // Initialize queue management handlers
        initQueueHandlers();

        // Mark initial page load
        if (window.history.state === null) {
            window.history.replaceState({ path: currentPath }, '', currentPath);
        }

        isInitialized = true;
        console.log('SPA Navigation initialized');
    }

    /**
     * Initialize hamburger menu with event delegation
     * This ensures it works reliably across all page transitions
     */
    function initHamburgerMenu() {
        // Remove any existing handlers first to avoid duplicates
        $(document).off('click', '.navbar-burger');

        // Bind with event delegation
        $(document).on('click', '.navbar-burger', function(e) {
            e.preventDefault();
            e.stopPropagation();
            $('.navbar-burger').toggleClass('is-active');
            $('.navbar-menu').toggleClass('is-active');
        });
    }

    /**
     * SmartokePy — DROPDOWN estilo datalist busca musica (abaixo/abaixo do elemento)
     * Mostra SOMENTE NOMES JA CADASTRADOS MANUALMENTE (nao nomes que vieram da fila).
     * Cabecalho + linhas clicaveis. Clique = seleciona. Clica fora = fecha.
     * Rodape SEMPRE tem INPUT TEXT + botao Confirmar (nunca depende de prompt()).
     */
    function openSingersDropdown(anchorEl, onSelectFn) {
        // 1) Fecha dropdown anterior se existir
        try {
            var old = document.getElementById("__singersDropdownBox");
            if (old) old.parentNode.removeChild(old);
        } catch (_) {}
        function closeDropdown() {
            try {
                var el = document.getElementById("__singersDropdownBox");
                if (el) el.parentNode.removeChild(el);
            } catch (_) {}
            try { document.removeEventListener("click", outsideClick); } catch (_) {}
            try { document.removeEventListener("keydown", escPress); } catch (_) {}
        }
        function outsideClick(evt) {
            try {
                var b = document.getElementById("__singersDropdownBox");
                if (!b) return;
                var tgt = evt.target;
                if (tgt && (tgt === anchorEl || b.contains(tgt) || anchorEl.contains(tgt))) return;
                closeDropdown();
            } catch (_) {}
        }
        function escPress(evt) { if (evt.keyCode === 27) closeDropdown(); }

        function applySelect(nomeFinal) {
            if (nomeFinal === undefined || nomeFinal === null) return;
            nomeFinal = String(nomeFinal).replace(/^\s+|\s+$/g, "");
            if (nomeFinal === "") return;
            closeDropdown();
            if (onSelectFn) onSelectFn(nomeFinal);
        }

        // 2) Cria box
        function buildBox(items /* [{name, label, isAlias}] */) {
            var temItens = items && items.length > 0;
            var rect = anchorEl.getBoundingClientRect();
            var scrollTop = window.pageYOffset || document.documentElement.scrollTop;
            var scrollLeft = window.pageXOffset || document.documentElement.scrollLeft;
            var box = document.createElement("div");
            box.id = "__singersDropdownBox";
            box.style.cssText = "position:absolute;z-index:99999;background-color:#ffffff;color:#0f172a;border:1px solid #334155;border-radius:6px;box-shadow:0 8px 26px rgba(0,0,0,.55);min-width:300px;max-width:92vw;max-height:62vh;overflow-y:auto;overflow-x:hidden;";
            var left = Math.max(6, Math.min((rect.left + scrollLeft) - 4, (scrollLeft + document.documentElement.clientWidth) - 310));
            var bottomGap = document.documentElement.clientHeight - rect.bottom;
            var dropHeight = 520;
            var top;
            if (bottomGap < 180 && rect.top > 200) {
                top = (rect.top + scrollTop) - dropHeight - 4;
                top = Math.max(scrollTop + 2, top);
            } else {
                top = (rect.bottom + scrollTop) + 4;
            }
            box.style.left = left + "px";
            box.style.top = top + "px";

            // Cabecalho estilo "Available songs..." como busca musica Toto
            var head = document.createElement("div");
            head.style.cssText = "padding:10px 14px;border-bottom:1px solid #e2e8f0;font-weight:700;font-size:13.5px;color:#1e293b;background-color:#f8fafc;letter-spacing:.2px;";
            head.textContent = "Cantores cadastrados manualmente";
            box.appendChild(head);

            if (temItens) {
                items.forEach(function (it) {
                    var row = document.createElement("div");
                    row.setAttribute("data-name", it.name);
                    row.style.cssText = "padding:10px 14px;border-bottom:1px solid #f1f5f9;cursor:pointer;font-size:14.5px;color:#0f172a;display:flex;align-items:center;gap:8px;-webkit-user-select:none;user-select:none;";
                    var icone = document.createElement("span");
                    icone.style.cssText = "font-weight:700;font-size:14px;";
                    icone.textContent = it.isAlias ? "🔗" : "👤";
                    icone.style.color = it.isAlias ? "#0ea5e9" : "#334155";
                    row.appendChild(icone);
                    var txt = document.createElement("span");
                    txt.textContent = it.label;
                    txt.style.flex = "1 1 auto";
                    row.appendChild(txt);
                    row.addEventListener("mouseenter", function(){ row.style.backgroundColor = "#eff6ff"; row.style.color = "#1e3a8a"; });
                    row.addEventListener("mouseleave", function(){ row.style.backgroundColor = ""; row.style.color = "#0f172a"; });
                    row.addEventListener("click", function (ev) {
                        ev.preventDefault();
                        ev.stopPropagation();
                        applySelect(row.getAttribute("data-name"));
                    });
                    box.appendChild(row);
                });
            } else {
                // Sem cadastrados: mensagem clara
                var empty = document.createElement("div");
                empty.style.cssText = "padding:14px 14px;border-bottom:1px solid #f1f5f9;background-color:#fffbeb;color:#78350f;font-size:13.5px;line-height:1.4;";
                var e1 = document.createElement("div"); e1.style.cssText = "font-weight:700;margin-bottom:4px;"; e1.textContent = "⚠️ Nenhum cantor cadastrado ainda.";
                var e2 = document.createElement("div"); e2.textContent = "Abra a aba CANTORES para cadastrar, OU digite um nome abaixo no campo e clique Confirmar.";
                empty.appendChild(e1); empty.appendChild(e2);
                box.appendChild(empty);
            }

            // Rodapé: INPUT TEXT + Botao Confirmar (NUNCA MAIS depende de prompt())
            var foot = document.createElement("div");
            foot.style.cssText = "padding:10px 12px;background-color:#f8fafc;border-top:1px solid #e2e8f0;display:flex;gap:6px;align-items:center;";
            var inp = document.createElement("input");
            inp.type = "text";
            inp.placeholder = "👉 Ou digite um nome aqui...";
            inp.className = "input is-small";
            inp.style.cssText = "flex:1 1 auto;";
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "button is-info is-small";
            btn.textContent = "Confirmar";
            function doFootConfirm() {
                var val = (inp.value || "").trim();
                if (val === "") { inp.focus(); return; }
                applySelect(val);
            }
            btn.addEventListener("click", function (ev) { ev.preventDefault(); ev.stopPropagation(); doFootConfirm(); });
            inp.addEventListener("keydown", function (ev) {
                if (ev.keyCode === 13) { ev.preventDefault(); ev.stopPropagation(); doFootConfirm(); }
            });
            foot.appendChild(inp);
            foot.appendChild(btn);
            box.appendChild(foot);

            document.body.appendChild(box);
            // Foca no input automaticamente para facilitar
            setTimeout(function(){ try { inp.focus(); } catch(_){} }, 50);

            setTimeout(function () {
                document.addEventListener("click", outsideClick);
                document.addEventListener("keydown", escPress);
            }, 0);
        }

        // Carrega os cadastrados manualmente (registered_singers, NAO unmatched)
        var registered;
        try {
            var now = Date.now();
            if (window.__SINGERS_CACHE && window.__SINGERS_CACHE.registered_singers &&
                (now - window.__SINGERS_CACHE.ts) < 15000) {
                registered = window.__SINGERS_CACHE.registered_singers;
            }
        } catch (_) { registered = null; }

        function buildFromRegistered(reg) {
            var items = [];
            if (Array.isArray(reg)) {
                reg.forEach(function (s) {
                    if (!s) return;
                    if (s.name) items.push({ name: String(s.name), label: String(s.name), isAlias: false });
                    if (Array.isArray(s.aliases_display)) {
                        s.aliases_display.forEach(function (al) {
                            if (al) items.push({ name: String(al), label: String(al), isAlias: true });
                        });
                    } else if (Array.isArray(s.aliases)) {
                        s.aliases.forEach(function (al) {
                            if (al) items.push({ name: String(al), label: String(al), isAlias: true });
                        });
                    }
                });
            }
            buildBox(items);
        }

        if (registered) {
            buildFromRegistered(registered);
            return;
        }

        // Fetch async
        var endpoint = "/api/singers/picker_options";
        var fallback = "/api/singers";
        var xhr = new XMLHttpRequest();
        xhr.open("GET", endpoint, true);
        xhr.setRequestHeader("Accept", "application/json");
        xhr.timeout = 10000;
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            if (xhr.status === 200) {
                try {
                    var j = JSON.parse(xhr.responseText);
                    var reg = j.registered_singers;
                    window.__SINGERS_CACHE = { ts: Date.now(), registered_singers: reg || [] };
                    if (Array.isArray(reg)) { buildFromRegistered(reg); return; }
                } catch (_) {}
            }
            // fallback: /api/singers
            var f2 = new XMLHttpRequest();
            f2.open("GET", fallback, true);
            f2.setRequestHeader("Accept", "application/json");
            f2.onreadystatechange = function () {
                if (f2.readyState !== 4) return;
                if (f2.status === 200) {
                    try {
                        var jj = JSON.parse(f2.responseText);
                        var list = Array.isArray(jj) ? jj : (jj.singers || jj.list || []);
                        window.__SINGERS_CACHE = { ts: Date.now(), registered_singers: list };
                        buildFromRegistered(list);
                        return;
                    } catch (_) {}
                }
                // Qualquer erro: monta box VAZIO (usuario ainda consegue digitar no input do footer)
                buildFromRegistered([]);
            };
            f2.send();
        };
        xhr.send();
    }
    // Expor globalmente para fallback/teste
    try { window.openSingersDropdown = openSingersDropdown; } catch (_) {}

    /**
     * Initialize username change handler with event delegation
     * This ensures it works reliably across all page transitions
     */
    function initUsernameHandler() {
        // Remove any existing handlers first to avoid duplicates
        $(document).off('click', '#current-user');

        // Bind with event delegation
        $(document).on('click', '#current-user', function(e) {
            e.preventDefault();
            e.stopPropagation();
            let anchor = this;
            try {
                openSingersDropdown(anchor, function(finalName) {
                    if (finalName === undefined || finalName === null || String(finalName).trim() === "") return;
                    Cookies.set("user", String(finalName).trim(), { expires: 3650, path: '/' });
                    if (typeof window.updateCurrentUserDisplay === "function") {
                        window.updateCurrentUserDisplay(String(finalName).trim());
                    } else {
                        $("#current-user span").text(String(finalName).trim());
                        $("#current-user").removeClass("is-hidden");
                    }
                });
            } catch (err) {
                let currentName = Cookies.get("user") || "";
                let nm = window.prompt("Quem esta usando esse dispositivo? Digite o nome.\nAtual: " + currentName);
                if (nm !== null && nm.trim() !== "") {
                    Cookies.set("user", nm, { expires: 3650, path: '/' });
                    if (typeof window.updateCurrentUserDisplay === "function") window.updateCurrentUserDisplay(nm);
                    else { $("#current-user span").text(nm); $("#current-user").removeClass("is-hidden"); }
                }
            }
            try { $(this).blur(); } catch (_) {}
        });
    }

    /**
     * Initialize queue management handlers with event delegation
     * This ensures they work reliably across all page transitions
     */
    function initQueueHandlers() {
        // Global flag to prevent rapid up/down clicks
        // Survives DOM regeneration caused by socket updates
        if (typeof window.queueButtonDebouncing === 'undefined') {
            window.queueButtonDebouncing = false;
        }

        // Remove any existing handlers first to avoid duplicates
        $(document).off('click', '.confirm-clear');
        $(document).off('click', '.confirm-delete');
        $(document).off('click', '.confirm-delete-file');
        $(document).off('click', '.up-button');
        $(document).off('click', '.down-button');
        $(document).off('click', '.add-random');

        // Clear queue confirmation
        $(document).on('click', '.confirm-clear', function(e) {
            e.preventDefault();
            var promptMsg = (window.translations && window.translations.promptClearQueue)
                ? window.translations.promptClearQueue
                : "Are you sure you want to clear the ENTIRE queue? Type 'ok' to continue";
            let userInput = window.prompt(promptMsg);
            // Only clear if user typed 'ok' exactly (case insensitive)
            if (userInput !== null && userInput.toLowerCase() === "ok") {
                $.get(this.href);
            }
        });

        // Delete song from queue confirmation
        $(document).on('click', '.confirm-delete', function(e) {
            e.preventDefault();
            var msg = (window.translations && window.translations.confirmDeleteFromQueue)
                ? window.translations.confirmDeleteFromQueue.replace('SONG_TITLE', this.title)
                : `Are you sure you want to delete "${this.title}" from the queue?`;
            if (window.confirm(msg)) {
                $.get(this.href);
            }
        });

        // Delete song file from library confirmation (full page navigation)
        $(document).on('click', '.confirm-delete-file', function(e) {
            e.preventDefault();
            var msg = (window.translations && window.translations.confirmDeleteFromLibrary)
                ? window.translations.confirmDeleteFromLibrary
                : 'Are you sure you want to delete this song from the library?';
            if (window.confirm(msg)) {
                window.location.href = this.href;
            }
        });

        // Move song up in queue
        $(document).on('click', '.up-button', function(e) {
            e.preventDefault();

            // Check global debounce flag - prevents all up/down clicks during debounce
            if (window.queueButtonDebouncing) {
                return;
            }

            // Set global debounce flag
            window.queueButtonDebouncing = true;

            // Visual feedback on all up/down buttons
            $('.up-button, .down-button').css('pointer-events', 'none').css('opacity', '0.5');

            $.get(this.href).always(function() {
                // Re-enable all buttons after request completes + 500ms
                setTimeout(function() {
                    $('.up-button, .down-button').css('pointer-events', 'auto').css('opacity', '1');
                    window.queueButtonDebouncing = false;
                }, 500);
            });
        });

        // Move song down in queue
        $(document).on('click', '.down-button', function(e) {
            e.preventDefault();

            // Check global debounce flag - prevents all up/down clicks during debounce
            if (window.queueButtonDebouncing) {
                return;
            }

            // Set global debounce flag
            window.queueButtonDebouncing = true;

            // Visual feedback on all up/down buttons
            $('.up-button, .down-button').css('pointer-events', 'none').css('opacity', '0.5');

            $.get(this.href).always(function() {
                // Re-enable all buttons after request completes + 500ms
                setTimeout(function() {
                    $('.up-button, .down-button').css('pointer-events', 'auto').css('opacity', '1');
                    window.queueButtonDebouncing = false;
                }, 500);
            });
        });

        // Add random songs to queue
        // SmartokePy: Usa a nova rota /queue/addrandom_api/N que retorna JSON {ok:true, added:N}
        // SEM redirect 302 full page HTML, evita "Conexão perdida" (SocketIO disconnectava
        // quando SPA hijack pegava redirect para /queue e recarregava DOM inteiro).
        $(document).on('click', '.add-random', function(e) {
            e.preventDefault();
            var raw = $('#randomNumberInput').val();
            var amount = parseInt(String(raw || "3").replace(/\D/g,''), 10);
            if (!amount || amount < 1) amount = 3;
            if (amount > 999) amount = 999;
            var btn = this;
            if (btn) {
                try {
                    btn.style.opacity = '0.4';
                    btn.style.pointerEvents = 'none';
                    setTimeout(function(){ try{btn.style.opacity='1'; btn.style.pointerEvents='auto';}catch(_){} }, 1200);
                } catch(_){}
            }
            $.get('/queue/addrandom_api/' + amount)
                .always(function(data){
                    try {
                        var j = (typeof data === "string") ? JSON.parse(data) : data;
                        if (j && typeof window.showNotification === "function") {
                            if (j.ok) {
                                window.showNotification(j.message || ("Adicionadas " + (j.added||amount) + " músicas aleatórias."), "is-success", 3000);
                            } else if (j.ran_out) {
                                window.showNotification(j.error || "Acabaram as músicas!", "is-warning", 3500);
                            } else {
                                window.showNotification(j.error || "Erro ao adicionar músicas aleatórias.", "is-danger", 4000);
                            }
                        }
                    } catch(e) {
                        if (typeof window.showNotification === "function") {
                            try { window.showNotification("Músicas aleatórias adicionadas.", "is-success", 2500); } catch(_){}
                        }
                    }
                });
        });
    }

    /**
     * Attach click listeners to all navigation links
     */
    function attachNavListeners() {
        $(document).on('click', config.linkSelector, function(e) {
            const href = $(this).attr('href');

            // Only intercept internal links that should use SPA navigation
            if (href && !href.startsWith('http') && !href.startsWith('#') && !shouldExcludeLink(this)) {
                e.preventDefault();
                navigateTo(href);
            }
        });
    }

    /**
     * Check if a link should be excluded from SPA navigation
     * Admin actions and system operations should do full page reloads
     * @param {HTMLElement} link - The link element
     * @returns {boolean}
     */
    function shouldExcludeLink(link) {
        const href = $(link).attr('href');
        const $link = $(link);

        // Exclude links with specific classes that use AJAX handlers
        if ($link.hasClass('no-spa') ||
            $link.hasClass('edit-button') ||
            $link.hasClass('add-song-link') ||  // Browse page add to queue
            $link.hasClass('confirm-clear') ||   // Clear queue button (has its own handler)
            $link.hasClass('confirm-delete') ||  // Delete song from queue (has its own handler)
            $link.hasClass('confirm-delete-file') ||  // Delete song file from library (has its own handler)
            $link.hasClass('up-button') ||       // Move song up button (has its own handler)
            $link.hasClass('down-button') ||     // Move song down button (has its own handler)
            $link.hasClass('add-random')) {      // Add random songs button (has its own handler)
            return true;
        }

        // Exclude admin action links that perform system operations
        const excludedPaths = [
            '/quit',
            '/shutdown',
            '/reboot',
            '/logout',
            '/login',
            '/update_ytdl',
            '/refresh',
            '/expand_fs',
            '/clear_preferences',
            '/auth',
            '/batch-song-renamer', // Edit all songs page
            '/files/edit', // Edit single song
            '/files/delete', // Delete song
            '/queue/edit' // Queue edit actions (move up/down/top/bottom/delete)
        ];

        // Check if the href matches any excluded path
        return excludedPaths.some(path => href && href.includes(path));
    }

    /**
     * Navigate to a new page dynamically
     * @param {string} url - The URL to navigate to
     * @param {boolean} addToHistory - Whether to add to browser history
     */
    async function navigateTo(url, addToHistory = true) {
        // Prevent concurrent navigations
        if (isNavigating) {
            return;
        }

        // Don't navigate if already on this page
        if (url === currentPath && addToHistory) {
            return;
        }

        isNavigating = true;

        // Close hamburger menu if open
        $('.navbar-burger').removeClass('is-active');
        $('.navbar-menu').removeClass('is-active');

        try {
            // Fetch the new page content with cache-busting to ensure fresh data
            const cacheBuster = Date.now();
            const separator = url.includes('?') ? '&' : '?';
            const fetchUrl = `${url}${separator}_=${cacheBuster}`;

            const response = await fetch(fetchUrl, {
                method: 'GET',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'text/html',
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache'
                }
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const html = await response.text();

            // Parse the HTML response
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');

            // Extract the new content
            const newContent = doc.querySelector(config.contentSelector);
            const newTitle = doc.querySelector('title');
            const newScripts = doc.querySelectorAll('script');
            const newStylesheets = doc.querySelectorAll('link[rel="stylesheet"]');
            const newInlineStyles = doc.querySelectorAll('style');

            if (newContent) {
                // Cleanup old scripts and event handlers
                cleanupOldPage();

                // Update the content
                $(config.contentSelector).html(newContent.innerHTML);

                // Update page title
                if (newTitle) {
                    document.title = newTitle.textContent;
                }

                // Update navigation highlighting
                updateNavHighlight(url);

                // Load external resources (CSS and JS) before executing inline scripts
                await loadExternalResources(newStylesheets, newScripts);

                // Inject inline styles from the new page
                injectInlineStyles(newInlineStyles);

                // Execute page-specific inline scripts
                executeScripts(newScripts);

                // Scroll to top
                window.scrollTo({ top: 0, behavior: config.scrollBehavior });

                // Update browser history
                if (addToHistory) {
                    window.history.pushState({ path: url }, '', url);
                }

                // Update current path
                currentPath = url;

                // Show success notification (optional, can be commented out)
                // showNotification('Page loaded', 'is-success', 500);

            } else {
                console.error('Could not find content container in response');
                // Fallback to normal navigation
                window.location.href = url;
            }

        } catch (error) {
            console.error('Navigation error:', error);
            // Fallback to normal navigation on error
            window.location.href = url;
        } finally {
            isNavigating = false;
        }
    }

    /**
     * Handle browser back/forward button
     * @param {PopStateEvent} event
     */
    function handlePopState(event) {
        if (event.state && event.state.path) {
            navigateTo(event.state.path, false);
        } else {
            // Fallback to full page reload if no state
            window.location.reload();
        }
    }

    /**
     * Update navbar active state highlighting
     * @param {string} url - The current URL (may include query params)
     */
    function updateNavHighlight(url) {
        // Extract base path without query parameters
        const path = url.split('?')[0];

        // Remove all active classes
        $('.navbar-item').removeClass('is-active');

        // Add active class to matching navbar item
        if (path === '/') {
            $('#home').addClass('is-active');
        } else if (path === '/queue') {
            $('#queue').addClass('is-active');
        } else if (path === '/search') {
            $('#search').addClass('is-active');
        } else if (path === '/browse' || path.startsWith('/browse')) {
            $('#browse').addClass('is-active');
        } else if (path === '/info') {
            $('#info').addClass('is-active');
        }
    }

    /**
     * Load external resources (CSS and JS files) from the new page
     * @param {NodeList} stylesheets - Link elements for stylesheets
     * @param {NodeList} scripts - Script elements
     * @returns {Promise} Resolves when all resources are loaded
     */
    async function loadExternalResources(stylesheets, scripts) {
        const loadPromises = [];

        // Load stylesheets
        stylesheets.forEach(link => {
            const href = link.getAttribute('href');
            if (href && !isResourceLoaded(href)) {
                loadPromises.push(loadStylesheet(href));
            }
        });

        // Load external scripts
        scripts.forEach(script => {
            const src = script.getAttribute('src');
            if (src && !isResourceLoaded(src)) {
                loadPromises.push(loadScript(src));
            }
        });

        // Wait for all resources to load
        await Promise.all(loadPromises);
    }

    /**
     * Check if a resource is already loaded
     * @param {string} url - The resource URL
     * @returns {boolean}
     */
    function isResourceLoaded(url) {
        // Normalize URL for comparison
        const normalizedUrl = url.split('?')[0]; // Remove query strings for comparison

        // Check if already tracked
        if (loadedResources.has(normalizedUrl)) {
            return true;
        }

        // Check if stylesheet already exists in DOM
        const existingStylesheet = document.querySelector(`link[href*="${normalizedUrl}"]`);
        if (existingStylesheet) {
            loadedResources.add(normalizedUrl);
            return true;
        }

        // Check if script already exists in DOM
        const existingScript = document.querySelector(`script[src*="${normalizedUrl}"]`);
        if (existingScript) {
            loadedResources.add(normalizedUrl);
            return true;
        }

        return false;
    }

    /**
     * Load a stylesheet dynamically
     * @param {string} href - The stylesheet URL
     * @returns {Promise}
     */
    function loadStylesheet(href) {
        return new Promise((resolve, reject) => {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = href;
            link.onload = () => {
                loadedResources.add(href.split('?')[0]);
                resolve();
            };
            link.onerror = () => {
                console.error(`Failed to load stylesheet: ${href}`);
                reject(new Error(`Failed to load stylesheet: ${href}`));
            };
            document.head.appendChild(link);
        });
    }

    /**
     * Load a script dynamically
     * @param {string} src - The script URL
     * @returns {Promise}
     */
    function loadScript(src) {
        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = src;
            script.onload = () => {
                loadedResources.add(src.split('?')[0]);
                resolve();
            };
            script.onerror = () => {
                console.error(`Failed to load script: ${src}`);
                reject(new Error(`Failed to load script: ${src}`));
            };
            document.head.appendChild(script);
        });
    }

    /**
     * Inject inline styles from the new page
     * @param {NodeList} styles - Style elements to inject
     */
    function injectInlineStyles(styles) {
        // Remove previously injected page-specific styles
        document.querySelectorAll('style[data-spa-injected]').forEach(style => {
            style.remove();
        });

        // Inject new inline styles
        styles.forEach(styleElement => {
            if (styleElement.textContent) {
                const newStyle = document.createElement('style');
                newStyle.textContent = styleElement.textContent;
                newStyle.setAttribute('data-spa-injected', 'true');
                document.head.appendChild(newStyle);
            }
        });
    }

    /**
     * Execute scripts from the new page
     * @param {NodeList} scripts - Script elements to execute
     */
    function executeScripts(scripts) {
        scripts.forEach(script => {
            // Only execute inline scripts
            // External scripts have already been loaded by loadExternalResources
            if (script.textContent && !script.src) {
                try {
                    // Create a new script element to ensure execution
                    const newScript = document.createElement('script');
                    newScript.textContent = script.textContent;

                    // Execute the script
                    document.body.appendChild(newScript);

                    // Clean up immediately
                    document.body.removeChild(newScript);
                } catch (error) {
                    console.error('Error executing script:', error);
                }
            }
        });
    }

    /**
     * Cleanup old page resources before loading new content
     */
    function cleanupOldPage() {
        // Remove old event handlers on elements that will be replaced
        $(config.contentSelector).off();

        // Note: We don't disconnect socket.io as it should persist across page changes
        // The socket connection is maintained globally
    }

    /**
     * Show a notification message
     * @param {string} message
     * @param {string} categoryClass
     * @param {number} timeout
     */
    function showNotification(message, categoryClass, timeout = 3000) {
        const notification = $(config.notificationSelector);
        notification.addClass(categoryClass);
        notification.find('div').text(message);
        notification.fadeIn();

        setTimeout(function() {
            notification.fadeOut();
        }, timeout);

        setTimeout(function() {
            notification.removeClass(categoryClass);
        }, timeout + 750);
    }

    // Initialize when DOM is ready
    $(document).ready(function() {
        init();
    });

})();
