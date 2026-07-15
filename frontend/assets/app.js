/**
 * economist_purifier · 前端应用逻辑
 * 纯原生 JS,无框架依赖,直接读取 window.economist_db
 */

(function () {
    'use strict';

    // ---------- 数据库读取 ----------
    const DB = Array.isArray(window.economist_db) ? window.economist_db : [];

    // ---------- marked 配置 ----------
    if (typeof marked !== 'undefined' && marked.setOptions) {
        marked.setOptions({
            breaks: true,
            gfm: true,
            headerIds: false,
            mangle: false,
        });
    }

    // ---------- 应用状态 ----------
    const state = {
        currentIssueId: null,
        currentArticleId: null,
        currentArticles: [],   // 当前 issue 的全部文章 (供全量搜索)
    };

    // ---------- 工具函数 ----------

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatDate(dateStr) {
        if (!dateStr) return '';
        const d = new Date(dateStr);
        if (isNaN(d.getTime())) return dateStr;
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    }

    function todayChinese() {
        const t = new Date();
        const opts = { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' };
        return t.toLocaleDateString('zh-CN', opts);
    }

    // ---------- 路由 ----------

    function parseHash() {
        const raw = window.location.hash.replace(/^#\/?/, '');
        const parts = raw.split('/').filter(Boolean);
        // 支持: #/issue/issue_xxx/art_xxx
        if (parts[0] === 'issue' && parts[1]) {
            return {
                issueId: parts[1],
                articleId: parts[2] || null,
            };
        }
        return { issueId: null, articleId: null };
    }

    function navigate(path, replace) {
        const hash = path.startsWith('#') ? path : '#' + path;
        if (replace) {
            // 替换当前历史记录 (例如初次进入默认文章时不留冗余 entry)
            const url = window.location.pathname + window.location.search + hash;
            window.history.replaceState(null, '', url);
            // 手动触发路由 (replaceState 不触发 hashchange)
            route();
        } else {
            window.location.hash = hash;
        }
    }

    // ---------- 视图切换 (含离开动画) ----------

    function switchView(viewId, onShown) {
        const current = document.querySelector('.view.is-active');
        const next = document.getElementById(viewId);
        if (!next) return;

        if (current && current !== next) {
            current.classList.add('is-leaving');
            // 等淡出动画结束
            setTimeout(() => {
                current.classList.remove('is-active', 'is-leaving');
                next.classList.add('is-active');
                if (typeof onShown === 'function') onShown();
            }, 220);
        } else if (!current) {
            next.classList.add('is-active');
            if (typeof onShown === 'function') onShown();
        } else {
            // 同 view 重新渲染
            if (typeof onShown === 'function') onShown();
        }
    }

    // ---------- 封面墙渲染 ----------

    function renderCoverCard(issue) {
        const coverRel = issue.issue_cover
            ? `${issue.issue_cover}`
            : null;
        const articleCount = (issue.articles && issue.articles.length) || 0;
        const dateLabel = formatDate(issue.issue_date);
        // 年份后两位作为封面回退字母
        const yearShort = (dateLabel.match(/^(\d{4})/) || ['', ''])[1].slice(2) || 'E';

        const coverImg = coverRel
            ? `<img class="cover-image" src="${escapeHtml(coverRel)}" `
              + `alt="${escapeHtml(issue.issue_id)}" loading="lazy" `
              + `onerror="this.style.display='none';`
              + `var fb=this.nextElementSibling;if(fb)fb.style.display='flex';">`
            : '';
        const fallbackStyle = coverRel ? 'display:none;' : '';

        return `
            <article class="cover-card" data-issue-id="${escapeHtml(issue.issue_id)}"
                                data-search-date="${escapeHtml(dateLabel)}"
                                data-search-id="${escapeHtml(issue.issue_id)}">
                <div class="cover-image-wrap">
                    ${coverImg}
                    <div class="cover-image-fallback" style="${fallbackStyle}">${escapeHtml(yearShort)}</div>
                </div>
                <div class="cover-body">
                    <div class="cover-date">${escapeHtml(dateLabel)}</div>
                    <div class="cover-id">${escapeHtml(issue.issue_id)}</div>
                    <div class="cover-meta">
                        <span class="cover-meta-count">${articleCount} 篇文章</span>
                        <span class="cover-meta-arrow">→</span>
                    </div>
                </div>
            </article>
        `;
    }

    function renderWall() {
        const grid = document.getElementById('cover-grid');
        const empty = document.getElementById('empty-state');

        if (!DB.length) {
            grid.innerHTML = '';
            empty.hidden = false;
            document.getElementById('stat-issues').textContent = '0';
            document.getElementById('stat-articles').textContent = '0';
            return;
        }
        empty.hidden = true;

        // 按 issue_date 降序,最新的在最前
        const issues = [...DB].sort((a, b) => {
            return new Date(b.issue_date).getTime() - new Date(a.issue_date).getTime();
        });

        const totalArticles = issues.reduce(
            (sum, i) => sum + ((i.articles && i.articles.length) || 0), 0
        );
        document.getElementById('stat-issues').textContent = issues.length;
        document.getElementById('stat-articles').textContent = totalArticles;

        grid.innerHTML = issues.map(renderCoverCard).join('');

        grid.querySelectorAll('.cover-card').forEach(card => {
            card.addEventListener('click', () => {
                const issueId = card.dataset.issueId;
                if (!issueId) return;
                navigate(`/issue/${issueId}`);
            });
        });
    }

    // ---------- 二级下钻渲染 ----------

    function renderArticleList(articles) {
        // 按 section 分组,保持原文顺序
        const groups = {};
        articles.forEach(art => {
            const sec = art.section || 'Standard Section';
            if (!groups[sec]) groups[sec] = [];
            groups[sec].push(art);
        });

        return Object.entries(groups).map(([section, items]) => `
            <div class="article-group">
                <div class="article-group-header">${escapeHtml(section)}</div>
                ${items.map(art => {
                    const isCartoon = (art.cartoon_images && art.cartoon_images.length > 0)
                        || (art.section && art.section.toLowerCase() === 'cartoon')
                        || /cartoon/i.test(art.title || '');
                    const isIndicators = (art.section && art.section.toLowerCase() === 'indicators')
                        || (art.indicator_images && art.indicator_images.length > 0);
                    const cls = [isCartoon && 'is-cartoon', isIndicators && 'is-indicators']
                        .filter(Boolean).join(' ');
                    const searchTitle = `${art.title_zh || ''} ${art.title || ''} ${art.section || ''}`;
                    return `
                    <button class="article-item ${cls}"
                            data-article-id="${escapeHtml(art.id)}"
                            data-search-title="${escapeHtml(searchTitle)}">
                        <div class="article-item-title-zh">${escapeHtml(art.title_zh || art.title)}</div>
                        <div class="article-item-title-en">${escapeHtml(art.title)}</div>
                        <span class="article-match-badge" data-match-info></span>
                    </button>
                `;
                }).join('')}
            </div>
        `).join('');
    }

    function renderArticle(article) {
        if (!article) return;

        document.getElementById('current-section').textContent =
            article.section || 'Standard Section';
        document.getElementById('current-title-zh').textContent =
            article.title_zh || article.title || '';
        document.getElementById('current-title-en').textContent =
            article.title || '';

        // 漫画专栏: 显示图片横幅
        const banner = document.getElementById('cartoon-banner');
        const imagesBox = document.getElementById('cartoon-images');
        const cartoons = article.cartoon_images || [];
        if (cartoons.length > 0) {
            imagesBox.innerHTML = cartoons.map(p =>
                `<img src="${escapeHtml(p)}" alt="${escapeHtml(article.title_zh || article.title)}" loading="lazy">`
            ).join('');
            banner.hidden = false;
        } else {
            imagesBox.innerHTML = '';
            banner.hidden = true;
        }

        // 经济指标图表画廊
        const indBanner = document.getElementById('indicators-banner');
        const indGallery = document.getElementById('indicators-gallery');
        const indicators = article.indicator_images || [];
        if (indicators.length > 0) {
            indGallery.innerHTML = indicators.map((it, idx) => {
                const path = typeof it === 'string' ? it : it.path;
                const caption = typeof it === 'string' ? '' : (it.caption || '');
                return `
                    <figure class="indicator-item" data-idx="${idx}">
                        <img src="${escapeHtml(path)}"
                             alt="${escapeHtml(caption || 'Indicator chart')}"
                             loading="lazy">
                        ${caption ? `<figcaption class="indicator-caption">${escapeHtml(caption)}</figcaption>` : ''}
                    </figure>
                `;
            }).join('');
            indBanner.hidden = false;
            // 绑定灯箱点击
            indGallery.querySelectorAll('.indicator-item').forEach((fig, idx) => {
                fig.addEventListener('click', () => openLightbox(indicators, idx));
            });
        } else {
            indGallery.innerHTML = '';
            indBanner.hidden = true;
        }

        // summary_md → 走 marked 渲染
        const summaryEl = document.getElementById('summary-content');
        if (typeof marked !== 'undefined') {
            try {
                summaryEl.innerHTML = marked.parse(article.summary_md || '');
            } catch (e) {
                summaryEl.textContent = article.summary_md || '';
            }
        } else {
            summaryEl.textContent = article.summary_md || '';
        }

        // 中英双栏对照阅读器
        renderBilingual(article);

        // 重置滚动
        summaryEl.scrollTop = 0;
    }

    // ============================================================
    //   中英逐段对照(平铺版)
    //   - 每个段落 = 一个 pair 行,左 ZH 右 EN,横向对齐
    //   - 所有 pair 平铺到页面,统一滚动,没有局部滚动条
    //   - 没有任何同步 / hover / click / active 联动
    // ============================================================
    function renderBilingual(article) {
        const paras = (article && Array.isArray(article.paragraphs)) ? article.paragraphs : [];
        const grid = document.getElementById('bilingual-grid');
        if (!grid) return;

        if (paras.length === 0) {
            grid.innerHTML = '<p class="bilingual-empty">(该文章暂无双语段落对照)</p>';
            return;
        }

        // 每个段对 = 1fr 1fr 内嵌 grid(左 ZH 右 EN)
        grid.innerHTML = paras.map(p => `
            <div class="bilingual-pair" data-para-id="${escapeHtml(p.para_id || '')}">
                <div class="bilingual-pair-zh">${p.zh_text || ''}</div>
                <div class="bilingual-pair-en">${p.en_html || ''}</div>
            </div>
        `).join('');
    }

    // ===== 灯箱逻辑 =====
    const lightboxState = { images: [], idx: 0 };

    function openLightbox(images, idx) {
        lightboxState.images = images;
        lightboxState.idx = idx;
        renderLightbox();
        document.getElementById('lightbox').hidden = false;
        document.body.style.overflow = 'hidden';
    }

    function closeLightbox() {
        document.getElementById('lightbox').hidden = true;
        document.body.style.overflow = '';
    }

    function renderLightbox() {
        const it = lightboxState.images[lightboxState.idx];
        if (!it) return;
        const path = typeof it === 'string' ? it : it.path;
        const caption = typeof it === 'string' ? '' : (it.caption || '');
        document.getElementById('lightbox-image').src = path;
        document.getElementById('lightbox-image').alt = caption || 'Indicator';
        document.getElementById('lightbox-caption').textContent = caption;
        document.getElementById('lightbox-counter').textContent =
            `${lightboxState.idx + 1} / ${lightboxState.images.length}`;
    }

    function lightboxPrev() {
        if (lightboxState.images.length === 0) return;
        lightboxState.idx = (lightboxState.idx - 1 + lightboxState.images.length)
            % lightboxState.images.length;
        renderLightbox();
    }

    function lightboxNext() {
        if (lightboxState.images.length === 0) return;
        lightboxState.idx = (lightboxState.idx + 1) % lightboxState.images.length;
        renderLightbox();
    }

    function setupLightbox() {
        document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
        document.getElementById('lightbox-prev').addEventListener('click', lightboxPrev);
        document.getElementById('lightbox-next').addEventListener('click', lightboxNext);
        // 点击背景关闭
        document.getElementById('lightbox').addEventListener('click', (e) => {
            if (e.target.id === 'lightbox') closeLightbox();
        });
        // 键盘 ←/→/Esc
        document.addEventListener('keydown', (e) => {
            const lb = document.getElementById('lightbox');
            if (lb.hidden) return;
            if (e.key === 'Escape') closeLightbox();
            else if (e.key === 'ArrowLeft') lightboxPrev();
            else if (e.key === 'ArrowRight') lightboxNext();
        });
    }

    function renderIssue(issueId, articleId) {
        const issue = DB.find(i => i.issue_id === issueId);
        if (!issue) {
            console.warn(`Issue ${issueId} not found, returning to wall`);
            navigate('/');
            return;
        }

        state.currentIssueId = issueId;
        state.currentArticles = issue.articles || [];

        // Toolbar 元信息
        document.getElementById('issue-id-label').textContent = issue.issue_id;
        document.getElementById('issue-date-label').textContent = formatDate(issue.issue_date);

        // 文章数统计
        const articleCount = (issue.articles && issue.articles.length) || 0;
        document.getElementById('article-count').textContent = articleCount + ' 篇';

        // 文章列表
        const list = document.getElementById('article-list');
        list.innerHTML = renderArticleList(issue.articles || []);

        // 绑定导航点击
        list.querySelectorAll('.article-item').forEach(item => {
            item.addEventListener('click', () => {
                const aid = item.dataset.articleId;
                if (!aid) return;
                navigate(`/issue/${issueId}/${aid}`);
            });
        });

        // 决定当前展示的文章
        let target = null;
        if (articleId) {
            target = (issue.articles || []).find(a => a.id === articleId);
        }
        if (!target && issue.articles && issue.articles.length) {
            target = issue.articles[0];
        }

        if (target) {
            renderArticle(target);
            state.currentArticleId = target.id;
            // 更新 URL (若缺 articleId)
            if (!articleId) {
                navigate(`/issue/${issueId}/${target.id}`, /* replace */ true);
            }
            // 高亮 nav 项
            list.querySelectorAll('.article-item').forEach(item => {
                item.classList.toggle(
                    'is-active',
                    item.dataset.articleId === target.id
                );
            });
            // 滚动选中项到视野内
            setTimeout(() => {
                const active = list.querySelector('.article-item.is-active');
                if (active) active.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }, 50);
        }

        // 滚动到顶部
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    // ---------- 路由分发 ----------

    function route() {
        const { issueId, articleId } = parseHash();

        if (issueId) {
            switchView('view-issue', () => renderIssue(issueId, articleId));
        } else {
            switchView('view-wall', () => renderWall());
        }
        document.body.classList.toggle('in-issue', !!issueId);
        updateFloatingBack();
    }

    // ---------- 悬浮返回按钮 ----------

    function updateFloatingBack() {
        const btn = document.getElementById('floating-back');
        if (!btn) return;
        const onIssue = !!parseHash().issueId;
        const scrolled = window.scrollY > 200;
        btn.classList.toggle('is-visible', onIssue && scrolled);
    }

    function setupFloatingBack() {
        const btn = document.getElementById('floating-back');
        if (!btn) return;
        btn.addEventListener('click', () => {
            if (parseHash().issueId) {
                navigate('/');
            } else {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        });
        window.addEventListener('scroll', updateFloatingBack, { passive: true });
    }

    // ===== Theme toggle =====
    const THEME_KEY = 'economist_purifier_theme';

    function getStoredTheme() {
        try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
    }

    function setStoredTheme(theme) {
        try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* ignore */ }
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        setStoredTheme(theme);
    }

    function initTheme() {
        const stored = getStoredTheme();
        let theme = stored;
        if (!theme) {
            // 跟随系统偏好
            theme = window.matchMedia('(prefers-color-scheme: dark)').matches
                ? 'dark' : 'light';
        }
        applyTheme(theme);
    }

    function setupThemeToggle() {
        const btn = document.getElementById('theme-toggle');
        if (!btn) return;
        btn.addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            const next = current === 'light' ? 'dark' : 'light';
            applyTheme(next);
        });
        // 监听系统主题变化 (用户未手动设置时响应)
        window.matchMedia('(prefers-color-scheme: dark)')
            .addEventListener('change', e => {
                if (!getStoredTheme()) {
                    applyTheme(e.matches ? 'dark' : 'light');
                }
            });
    }

    // ===== Mobile section nav drawer =====
    function setupMobileNavToggle() {
        const toggle = document.getElementById('mobile-nav-toggle');
        const nav = document.querySelector('.section-nav');
        if (!toggle || !nav) return;

        const setOpen = (open) => {
            nav.classList.toggle('is-open', open);
            toggle.classList.toggle('is-active', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        };

        toggle.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const open = !nav.classList.contains('is-open');
            setOpen(open);
        });

        // 点击文章后自动收起
        document.addEventListener('click', (e) => {
            if (e.target.closest('.article-item') && nav.classList.contains('is-open')) {
                setOpen(false);
            }
        });
    }

    // ===== Mobile FAB drawer =====
    function setupMobileNavFab() {
        const fab = document.getElementById('mobile-nav-fab');
        const nav = document.querySelector('.section-nav');
        if (!fab || !nav) return;

        // 创建 backdrop
        const backdrop = document.createElement('div');
        backdrop.className = 'nav-drawer-backdrop';
        document.body.appendChild(backdrop);

        const setOpen = (open) => {
            nav.classList.toggle('is-open', open);
            fab.classList.toggle('is-active', open);
            backdrop.classList.toggle('is-visible', open);
            document.body.classList.toggle('nav-open', open);
            fab.setAttribute('aria-expanded', open ? 'true' : 'false');
        };

        fab.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const open = !nav.classList.contains('is-open');
            setOpen(open);
        });

        // 点击 backdrop 关闭
        backdrop.addEventListener('click', () => setOpen(false));

        // ★ 选中文章后不再自动折叠 — 让用户连续浏览, 抽屉保持打开
        //    用户点 FAB 再次 / 点 backdrop / 按 Esc 才关闭
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && nav.classList.contains('is-open')) {
                setOpen(false);
            }
        });
    }

    // 视图切换时同步 body class (用于显示/隐藏 FAB)
    function syncBodyIssueClass() {
        const issueView = document.getElementById('view-issue');
        if (issueView && issueView.classList.contains('is-active')) {
            document.body.classList.add('in-issue');
        } else {
            document.body.classList.remove('in-issue');
        }
    }

    // ===== Search =====
    function setupWallSearch() {
        const input = document.getElementById('wall-search');
        const clear = document.getElementById('wall-search-clear');
        const grid = document.getElementById('cover-grid');
        if (!input || !clear || !grid) return;

        const apply = () => {
            const q = input.value.trim().toLowerCase();
            clear.hidden = !q;
            let visible = 0;
            grid.querySelectorAll('.cover-card').forEach(card => {
                const date = card.dataset.searchDate || '';
                const id = card.dataset.searchId || '';
                const match = !q || date.includes(q) || id.toLowerCase().includes(q);
                card.classList.toggle('is-hidden', !match);
                if (match) visible++;
            });
            grid.classList.toggle('is-empty', visible === 0);
        };

        input.addEventListener('input', apply);
        clear.addEventListener('click', () => {
            input.value = '';
            apply();
            input.focus();
        });
        // Esc 清空
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                input.value = '';
                apply();
            }
        });
    }

    function setupNavSearch() {
        const input = document.getElementById('nav-search');
        const clear = document.getElementById('nav-search-clear');
        const list = document.getElementById('article-list');
        const empty = document.getElementById('nav-search-empty');
        if (!input || !clear || !list || !empty) return;

        // 提取纯文本 (剥离 HTML 标签, 用于正文搜索)
        const stripHtml = (html) => (html || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

        const apply = () => {
            const q = input.value.trim().toLowerCase();
            clear.hidden = !q;
            let totalVisible = 0;

            // ★ 关键: 每次搜索都重新构建索引 (state.currentArticles 在 init 时为空)
            const articleIndex = new Map();
            (state.currentArticles || []).forEach(art => articleIndex.set(art.id, art));

            list.querySelectorAll('.article-group').forEach(group => {
                let groupVisible = 0;
                group.querySelectorAll('.article-item').forEach(item => {
                    const aid = item.dataset.articleId;
                    const art = articleIndex.get(aid);
                    let matched = false;
                    let matchInfo = '';

                    if (!q) {
                        matched = true;
                    } else if (art) {
                        const fields = [
                            { key: 'title_zh', text: art.title_zh || '' },
                            { key: 'title', text: art.title || '' },
                            { key: 'section', text: art.section || '' },
                            { key: 'summary_md', text: art.summary_md || '' },
                            { key: 'content_raw', text: stripHtml(art.content_raw) },
                        ];
                        const matchedFields = fields.filter(f => f.text.toLowerCase().includes(q));
                        if (matchedFields.length > 0) {
                            matched = true;
                            const noteworthy = matchedFields.filter(f =>
                                f.key === 'summary_md' || f.key === 'content_raw');
                            if (noteworthy.length > 0) {
                                matchInfo = `✦ ${noteworthy.map(f =>
                                    f.key === 'summary_md' ? '研报' : '正文').join('+')}`;
                            }
                        }
                    }

                    item.classList.toggle('is-hidden', !matched);

                    const badge = item.querySelector('.article-match-badge');
                    if (badge) {
                        badge.textContent = matchInfo;
                        badge.classList.toggle('is-visible', !!matchInfo);
                    }

                    if (matched) {
                        groupVisible++;
                        totalVisible++;
                    }
                });
                group.classList.toggle('is-hidden', groupVisible === 0);
            });
            empty.hidden = totalVisible > 0 || !q;
        };

        input.addEventListener('input', apply);
        clear.addEventListener('click', () => {
            input.value = '';
            apply();
            input.focus();
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                input.value = '';
                apply();
            }
        });
    }

    // ---------- Init ----------

    function init() {
        initTheme();

        // 报头日期
        document.getElementById('masthead-date').textContent = todayChinese();
        document.getElementById('footer-year').textContent = new Date().getFullYear();

        // 返回按钮
        const btnBack = document.getElementById('btn-back');
        if (btnBack) btnBack.addEventListener('click', () => navigate('/'));

        setupFloatingBack();
        setupThemeToggle();
        setupLightbox();
        setupMobileNavToggle();
        setupMobileNavFab();
        setupWallSearch();
        setupNavSearch();

        // 中英对照平铺版:无任何事件绑定,无需 setup

        // 首次渲染
        if (!document.querySelector('.view.is-active')) {
            const { issueId } = parseHash();
            document.getElementById(issueId ? 'view-issue' : 'view-wall')
                .classList.add('is-active');
        }

        route();
        window.addEventListener('hashchange', route);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();