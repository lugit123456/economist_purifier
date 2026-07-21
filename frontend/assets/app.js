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

    function scrollDocumentToTop() {
        const forceTop = () => {
            const scrollingElement = document.scrollingElement || document.documentElement;
            if (scrollingElement) scrollingElement.scrollTop = 0;
            document.documentElement.scrollTop = 0;
            document.body.scrollTop = 0;
            window.scrollTo(0, 0);
        };

        forceTop();
        requestAnimationFrame(forceTop);
        setTimeout(forceTop, 120);
        setTimeout(forceTop, 320);
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

        // ★ 绑图点击 → 灯箱 (单图放大, 桌面 + 移动)
        bindImageZoom(article);

        // 重置滚动
        summaryEl.scrollTop = 0;

        // 更新正文末尾的上一篇/下一篇文章导航
        updateArticleNavigation();
    }

    // 单图点击放大 (复用现有灯箱, 单图模式)
    function bindImageZoom(_article) {
        const view = document.getElementById('view-issue');
        if (!view) return;
        // 已经绑定过的不重复绑 (用 WeakSet 标记)
        view.querySelectorAll('img').forEach(img => {
            if (img.dataset.zoomBound === '1') return;
            img.dataset.zoomBound = '1';
            // cursor: zoom-in 是桌面端的视觉提示
            img.style.cursor = 'zoom-in';
            img.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                // ★ 单图模式: 数组里只放一张, prev/next 自动隐藏
                const src = img.currentSrc || img.src;
                const alt = img.alt || '';
                const figcaption = img.closest('figure')?.querySelector('figcaption')?.textContent?.trim() || '';
                const caption = figcaption || alt;
                openLightbox([{ path: src, caption: caption }], 0);
            });
        });
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
        document.getElementById('lightbox-image').alt = caption || '图片';
        document.getElementById('lightbox-caption').textContent = caption;
        const total = lightboxState.images.length;
        document.getElementById('lightbox-counter').textContent =
            total > 1 ? `${lightboxState.idx + 1} / ${total}` : '';
        // ★ 单图模式 (普通文章里的 img): 隐藏 prev/next 按钮
        const multi = total > 1;
        const prevBtn = document.getElementById('lightbox-prev');
        const nextBtn = document.getElementById('lightbox-next');
        if (prevBtn) prevBtn.style.display = multi ? '' : 'none';
        if (nextBtn) nextBtn.style.display = multi ? '' : 'none';
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
        const bottomToc = document.getElementById('article-bottom-toc');
        const bottomTocCount = document.getElementById('article-bottom-toc-count');
        if (bottomTocCount) bottomTocCount.textContent = `本期共 ${articleCount} 篇文章`;
        if (bottomToc) bottomToc.setAttribute('aria-label', `查看本期目录，共 ${articleCount} 篇文章`);

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
            // 先设 state，让 renderArticle 能读取正确的上一篇/下一篇文章
            state.currentArticleId = target.id;
            renderArticle(target);
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

        // hash 切换文章时浏览器会保留旧滚动位置，渲染后强制回到页面顶端。
        scrollDocumentToTop();
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
        updateArticleNavigation();
    }

    // ---------- 文章上一篇 / 下一篇导航 ----------

    function navigateToArticle(articleId) {
        const issueId = state.currentIssueId;
        if (!issueId || !articleId) return;
        navigate(`/issue/${issueId}/${articleId}`);
    }

    function updateArticleNavigation() {
        const prevBtn = document.getElementById('article-nav-prev');
        const nextBtn = document.getElementById('article-nav-next');
        if (!prevBtn || !nextBtn) return;
        const onIssue = !!parseHash().issueId;
        const articles = state.currentArticles || [];
        const idx = articles.findIndex(a => a.id === state.currentArticleId);
        const hasPrev = idx > 0;
        const hasNext = idx >= 0 && idx < articles.length - 1;

        const prevArticle = hasPrev ? articles[idx - 1] : null;
        const nextArticle = hasNext ? articles[idx + 1] : null;
        const prevTitle = document.getElementById('article-nav-prev-title');
        const nextTitle = document.getElementById('article-nav-next-title');

        prevBtn.classList.toggle('is-disabled', !hasPrev);
        nextBtn.classList.toggle('is-disabled', !hasNext);
        prevBtn.classList.toggle('is-visible', onIssue);
        nextBtn.classList.toggle('is-visible', onIssue);
        prevBtn.disabled = !hasPrev;
        nextBtn.disabled = !hasNext;
        prevBtn.setAttribute('aria-label', hasPrev
            ? `上一篇文章：${prevArticle.title_zh || prevArticle.title || ''}`
            : '已是本期第一篇文章');
        nextBtn.setAttribute('aria-label', hasNext
            ? `下一篇文章：${nextArticle.title_zh || nextArticle.title || ''}`
            : '已是本期最后一篇文章');
        if (prevTitle) {
            prevTitle.textContent = hasPrev
                ? (prevArticle.title_zh || prevArticle.title || '上一篇文章')
                : '已是本期第一篇';
        }
        if (nextTitle) {
            nextTitle.textContent = hasNext
                ? (nextArticle.title_zh || nextArticle.title || '下一篇文章')
                : '已是本期最后一篇';
        }
    }

    function setupArticleNavigation() {
        const prevBtn = document.getElementById('article-nav-prev');
        const nextBtn = document.getElementById('article-nav-next');
        if (!prevBtn || !nextBtn) return;

        // 切文章前先停掉朗读 (TTS.stop() 会重置按钮图标/文字)
        const stopTtsIfActive = () => {
            if (typeof TTS !== 'undefined' && TTS.getState && TTS.getState() !== 'idle') {
                TTS.stop();
            }
        };

        prevBtn.addEventListener('click', () => {
            stopTtsIfActive();
            const articles = state.currentArticles || [];
            const idx = articles.findIndex(a => a.id === state.currentArticleId);
            if (idx > 0) navigateToArticle(articles[idx - 1].id);
        });
        nextBtn.addEventListener('click', () => {
            stopTtsIfActive();
            const articles = state.currentArticles || [];
            const idx = articles.findIndex(a => a.id === state.currentArticleId);
            if (idx >= 0 && idx < articles.length - 1) {
                navigateToArticle(articles[idx + 1].id);
            }
        });
    }

    // ========== TOC 底部弹层 ==========
    function openTocSheet() {
        const sheet = document.getElementById('toc-sheet');
        const body = document.getElementById('toc-sheet-body');
        if (!sheet || !body) return;
        // 渲染本期文章列表 (复用 renderArticleList)
        const articles = state.currentArticles || [];
        body.innerHTML = renderArticleList(articles);
        // 高亮当前文章
        const currentId = state.currentArticleId;
        body.querySelectorAll('.article-item').forEach(item => {
            if (item.dataset.articleId === currentId) {
                item.classList.add('is-active');
                // 滚到可视区
                setTimeout(() => item.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80);
            }
        });
        // 文章点击 → 跳转 + 关闭弹层
        body.querySelectorAll('.article-item').forEach(item => {
            item.addEventListener('click', () => {
                const aid = item.dataset.articleId;
                if (!aid || aid === currentId) {
                    closeTocSheet();
                    return;
                }
                closeTocSheet();
                const issueId = state.currentIssueId;
                navigate(`/issue/${issueId}/${aid}`);
            });
        });
        sheet.hidden = false;
        // 强制 reflow 让 transition 生效
        void sheet.offsetWidth;
        sheet.classList.add('is-open');
        document.querySelectorAll('[data-toc-trigger]').forEach(trigger => {
            trigger.classList.add('is-active');
            trigger.setAttribute('aria-expanded', 'true');
        });
        document.body.style.overflow = 'hidden';
    }

    function closeTocSheet() {
        const sheet = document.getElementById('toc-sheet');
        if (!sheet) return;
        sheet.classList.remove('is-open');
        // 等 transition 结束再 hidden
        setTimeout(() => {
            if (!sheet.classList.contains('is-open')) {
                sheet.hidden = true;
            }
        }, 300);
        document.querySelectorAll('[data-toc-trigger]').forEach(trigger => {
            trigger.classList.remove('is-active');
            trigger.setAttribute('aria-expanded', 'false');
        });
        document.body.style.overflow = '';
    }

    function setupTocSheet() {
        const sheet = document.getElementById('toc-sheet');
        const backdrop = document.getElementById('toc-sheet-backdrop');
        const closeBtn = document.getElementById('toc-sheet-close');
        if (!sheet) return;
        backdrop && backdrop.addEventListener('click', closeTocSheet);
        closeBtn && closeBtn.addEventListener('click', closeTocSheet);
        // Esc 关闭
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && sheet.classList.contains('is-open')) {
                closeTocSheet();
            }
        });
    }

    // ========== 返回文章顶部（滚动一段距离后显示） ==========
    function setupBackToTop() {
        const btn = document.getElementById('back-to-top');
        if (!btn) return;

        const SHOW_AFTER = 520;

        const update = () => {
            if (!document.body.classList.contains('in-issue')) {
                btn.classList.remove('is-visible');
                return;
            }
            const scrollTop = window.scrollY || document.documentElement.scrollTop;
            if (scrollTop > SHOW_AFTER) {
                btn.classList.add('is-visible');
            } else {
                btn.classList.remove('is-visible');
            }
        };

        window.addEventListener('scroll', update, { passive: true });
        window.addEventListener('resize', update);
        btn.addEventListener('click', () => {
            btn.classList.remove('is-visible');
            btn.blur();
            scrollDocumentToTop();
        });
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

    // ===== Mobile issue directory =====
    function setupMobileNavToggle() {
        const triggers = document.querySelectorAll('[data-toc-trigger]');
        if (!triggers.length) return;

        triggers.forEach(trigger => {
            trigger.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const sheet = document.getElementById('toc-sheet');
                if (sheet && sheet.classList.contains('is-open')) {
                    closeTocSheet();
                } else {
                    openTocSheet();
                }
            });
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

    // ---------- TTS 朗读 (浏览器原生 Web Speech API) ----------

    const TTS = (() => {
        const synth = ('speechSynthesis' in window) ? window.speechSynthesis : null;
        let voicesCache = [];
        let voicesReady = false;
        let currentBtn = null;
        let currentUtter = null;
        let voiceRetryHandle = null;
        // 状态机: idle / playing / paused
        let state = 'idle';

        // 用户在音色面板里的选择 (持久化到 localStorage)
        const PREF_KEY = 'economist_purifier_tts_pref';
        let pref = { voiceZh: '', voiceEn: '', rate: 1.0 };
        try {
            const saved = localStorage.getItem(PREF_KEY);
            if (saved) Object.assign(pref, JSON.parse(saved));
        } catch (e) {}

        function savePref() {
            try { localStorage.setItem(PREF_KEY, JSON.stringify(pref)); } catch (e) {}
        }

        function loadVoices() {
            if (!synth) return [];
            voicesCache = synth.getVoices() || [];
            voicesReady = voicesCache.length > 0;
            return voicesCache;
        }

        // iOS/Safari 上 voices 是异步加载的, 必须等 voiceschanged
        if (synth) {
            loadVoices();
            synth.addEventListener && synth.addEventListener('voiceschanged', () => {
                loadVoices();
            });
            // 兜底轮询: 部分实现不发 voiceschanged
            if (!voicesReady) {
                voiceRetryHandle = setInterval(() => {
                    loadVoices();
                    if (voicesReady && voiceRetryHandle) {
                        clearInterval(voiceRetryHandle);
                        voiceRetryHandle = null;
                    }
                }, 250);
                // 5 秒后强制结束轮询 (避免永远跑)
                setTimeout(() => {
                    if (voiceRetryHandle) {
                        clearInterval(voiceRetryHandle);
                        voiceRetryHandle = null;
                    }
                }, 5000);
            }
        }

        function pickVoice(langPrefix) {
            if (!voicesCache.length) loadVoices();
            const prefix = langPrefix.toLowerCase();
            // 用户在面板里手动选了 → 优先用它
            const prefName = prefix.startsWith('zh') ? pref.voiceZh : prefix.startsWith('en') ? pref.voiceEn : '';
            if (prefName) {
                const exact = voicesCache.find(v => v.name === prefName);
                if (exact) return exact;
            }
            const candidates = voicesCache.filter(v => v.lang && v.lang.toLowerCase().startsWith(prefix));
            if (candidates.length === 0) return null;
            // 单个直接用
            if (candidates.length === 1) return candidates[0];

            // ★ 关键: 系统默认 eSpeak/compact voice 听起来非常机械
            //    优先级排序: 在线 > 自然/神经 > Google/Microsoft > 默认排序
            const VOICE_KEYWORDS = /(natural|neural|premium|enhanced|wavenet|online|journey|studio)/i;
            const PROVIDER_BONUS = /(google|microsoft|amazon|apple|naturalreaders|cereproc|nuance)/i;
            const score = (v) => {
                const name = (v.name || '');
                let s = 0;
                if (v.localService === false) s += 100;        // 在线 voice 通常更清楚
                if (VOICE_KEYWORDS.test(name)) s += 50;
                if (PROVIDER_BONUS.test(name)) s += 30;
                if (v.default) s += 10;
                return s;
            };
            candidates.sort((a, b) => score(b) - score(a));
            return candidates[0];
        }

        function stop() {
            if (!synth) return;
            try { synth.cancel(); } catch (e) {}
            state = 'idle';
            if (currentBtn) {
                currentBtn.classList.remove('is-playing');
                const icon = currentBtn.querySelector('.tts-icon');
                const lbl = currentBtn.querySelector('.tts-label');
                if (icon) icon.textContent = '🔊';
                if (lbl) lbl.textContent = lbl.dataset.ttsDefault || '朗读';
                currentBtn = null;
            }
            currentUtter = null;
        }

        function pause() {
            if (!synth) return false;
            if (state !== 'playing') return false;
            try { synth.pause(); } catch (e) { return false; }
            state = 'paused';
            if (currentBtn) {
                const icon = currentBtn.querySelector('.tts-icon');
                const lbl = currentBtn.querySelector('.tts-label');
                if (icon) icon.textContent = '▶';
                if (lbl) lbl.textContent = '继续';
            }
            return true;
        }

        function resume() {
            if (!synth) return false;
            if (state !== 'paused') return false;
            try { synth.resume(); } catch (e) { return false; }
            state = 'playing';
            if (currentBtn) {
                const icon = currentBtn.querySelector('.tts-icon');
                const lbl = currentBtn.querySelector('.tts-label');
                if (icon) icon.textContent = '⏸';
                if (lbl) lbl.textContent = '暂停';
            }
            return true;
        }

        function getState() { return state; }

        function speak(text, opts) {
            if (!synth) {
                showTip('此浏览器不支持 Web Speech API');
                return false;
            }
            if (!text || !text.trim()) {
                showTip('(无内容可朗读)');
                return false;
            }
            const btn = opts && opts.button;
            const lang = (opts && opts.lang) || 'zh-CN';
            const voice = pickVoice(lang);

            // 点击的是当前正在播放的按钮 → 停止
            if (currentBtn && btn && currentBtn === btn) {
                stop();
                return true;
            }

            // 已经在播放别的 → 停掉旧的再开始
            if (synth.speaking || synth.pending) {
                try { synth.cancel(); } catch (e) {}
                // Chrome bug: cancel 后立刻 speak 会被吞, 等 100ms
                setTimeout(() => doSpeak(text, lang, voice, btn), 100);
                return true;
            }

            // 没有在播放 → 直接 speak
            doSpeak(text, lang, voice, btn);
            return true;
        }

        // Chrome 对超长 utterance 有截断 bug, 按句末切块
        function chunkText(text, maxLen) {
            const chunks = [];
            const sentenceRe = /[^。！？.!?\n]+[。！？.!?]?/g;
            const sentences = text.match(sentenceRe) || [text];
            let cur = '';
            for (const s of sentences) {
                if ((cur + s).length > maxLen && cur) {
                    chunks.push(cur.trim());
                    cur = s;
                } else {
                    cur += s;
                }
            }
            if (cur.trim()) chunks.push(cur.trim());
            if (chunks.length === 1 && chunks[0].length > maxLen * 2) {
                const big = chunks[0];
                chunks.length = 0;
                for (let i = 0; i < big.length; i += maxLen) {
                    chunks.push(big.slice(i, i + maxLen));
                }
            }
            return chunks.length ? chunks : [text];
        }

        function doSpeak(text, lang, voice, btn) {
            const chunks = chunkText(text, 180);
            const rate = pref.rate || (lang.startsWith('zh') ? 0.95 : 1.0);
            let startedAny = false;

            function playChunk(idx) {
                if (idx >= chunks.length) {
                    stop();
                    return;
                }
                const utter = new SpeechSynthesisUtterance(chunks[idx]);
                utter.lang = lang;
                utter.rate = rate;
                utter.pitch = 1.0;
                if (voice) utter.voice = voice;
                utter.onstart = () => {
                    state = 'playing';
                    if (btn && !startedAny) {
                        startedAny = true;
                        currentBtn = btn;
                        btn.classList.add('is-playing');
                        const icon = btn.querySelector('.tts-icon');
                        const lbl = btn.querySelector('.tts-label');
                        if (icon) icon.textContent = '⏸';
                        if (lbl) {
                            if (!lbl.dataset.ttsDefault) lbl.dataset.ttsDefault = lbl.textContent;
                            lbl.textContent = '暂停';
                        }
                    }
                };
                utter.onpause = () => { state = 'paused'; };
                utter.onresume = () => { state = 'playing'; };
                utter.onend = () => playChunk(idx + 1);
                utter.onerror = (e) => {
                    console.warn('TTS chunk onerror', e);
                    if (e && e.error && e.error !== 'interrupted' && e.error !== 'canceled') {
                        showTip('朗读失败 (' + e.error + ')。\n'
                            + '可能原因: 浏览器 TTS 引擎未挂载 / 系统语音包缺失。\n'
                            + '建议: 系统设置 → 语言 → 添加中文语音包后刷新。');
                    }
                    stop();
                };
                currentUtter = utter;
                try {
                    synth.speak(utter);
                } catch (e) {
                    console.warn('TTS chunk speak failed', e);
                    stop();
                    showTip('朗读失败, 请检查浏览器 TTS 设置');
                }
            }

            playChunk(0);

            // 不再搞"1.5 秒静音检测"了 — Chrome Android 的 synth.speaking 状态不稳定,
            //   这个判断会产生误报 (明明有声音, 1.5s 后却被提示"引擎未挂载").
            //   只靠 onerror 和同步抛错来检测失败, 浏览器自己说了算, 稳定.
        }

        // 列出指定语言的可用 voice, 给上层 voice picker 用
        function listVoices(langPrefix) {
            if (!voicesCache.length) loadVoices();
            const prefix = (langPrefix || '').toLowerCase();
            return voicesCache
                .filter(v => v.lang && v.lang.toLowerCase().startsWith(prefix))
                .map(v => ({ name: v.name, lang: v.lang, voice: v }));
        }

        function supported() {
            return !!synth;
        }

        function hasAnyVoice() {
            if (!synth) return false;
            if (!voicesCache.length) loadVoices();
            return voicesCache.length > 0;
        }

        function tipElement() {
            let el = document.getElementById('tts-tip');
            if (!el) {
                el = document.createElement('div');
                el.id = 'tts-tip';
                el.style.cssText = [
                    'position:fixed', 'left:50%', 'bottom:80px',
                    'transform:translateX(-50%)',
                    'background:rgba(0,0,0,0.82)', 'color:#fff',
                    'padding:10px 16px', 'border-radius:8px',
                    'font-family:var(--font-ui)', 'font-size:13px',
                    'max-width:90vw', 'white-space:pre-line',
                    'text-align:center', 'z-index:999',
                    'opacity:0', 'transition:opacity .25s',
                    'pointer-events:none',
                ].join(';');
                document.body.appendChild(el);
            }
            return el;
        }
        let tipHideTimer = null;
        function showTip(msg) {
            const el = tipElement();
            el.textContent = msg;
            el.style.opacity = '1';
            if (tipHideTimer) clearTimeout(tipHideTimer);
            tipHideTimer = setTimeout(() => {
                el.style.opacity = '0';
            }, 3500);
        }

        return { speak, stop, pause, resume, getState, getCurrentBtn: () => currentBtn,
            supported, hasAnyVoice, showTip, voicesReady: () => voicesReady,
            listVoices, savePref, getPref: () => pref };
    })();

    // 抽取中英对照 zh / en 全部段落文本
    function collectBilingualText(lang) {
        const grid = document.getElementById('bilingual-grid');
        if (!grid) return '';
        const sel = lang === 'en' ? '.bilingual-pair-en' : '.bilingual-pair-zh';
        const parts = [];
        grid.querySelectorAll(sel).forEach(node => {
            // 跳过图片占位段 (chart 段含 <img>, 用 data-chart-id 标识)
            const figure = node.querySelector('figure.chart-figure');
            if (figure) {
                const alt = figure.querySelector('img')?.getAttribute('alt') || '';
                const cap = figure.querySelector('figcaption')?.textContent || '';
                // chart 段 zh_text 由 LLM 在左侧面板, 这里 alt/cap 仅作上下文, 不强制朗读
                if (lang === 'en') {
                    const txt = [alt, cap].filter(Boolean).join('. ');
                    if (txt) parts.push(txt);
                }
                return;
            }
            const txt = node.textContent.trim();
            if (txt) parts.push(txt);
        });
        return parts.join('\n\n');
    }

    function collectSummaryText() {
        const el = document.getElementById('summary-content');
        if (!el) return '';
        return el.textContent.trim();
    }

    function setupTtsButtons() {
        const bindings = [
            { id: 'tts-summary', getText: collectSummaryText, lang: 'zh-CN' },
            { id: 'tts-bilingual-zh', getText: () => collectBilingualText('zh'), lang: 'zh-CN' },
            { id: 'tts-bilingual-en', getText: () => collectBilingualText('en'), lang: 'en-US' },
        ];
        bindings.forEach(b => {
            const btn = document.getElementById(b.id);
            if (!btn) return;
            btn.addEventListener('click', () => {
                if (!TTS.supported()) {
                    TTS.showTip('此浏览器不支持 Web Speech API。\n'
                        + '请用 Chrome / Edge / Safari / UC 等现代浏览器打开本站。');
                    return;
                }
                // ★ 去掉 hasAnyVoice() 预检 — 改成"先尝试 speak, 再看 onerror"
                //   UC / 小米 / 部分国产浏览器的 TTS 引擎不走 getVoices() 接口
                //   预检 voices 列表会把它们误杀, 让用户失去可用引擎
                const s = TTS.getState();
                // paused → 继续 (按按钮的当前 text 判断, 因为 paused 时 onstart 不会重跑)
                if (s === 'paused' && btn === TTS.getCurrentBtn()) {
                    TTS.resume();
                    return;
                }
                // playing 本按钮 → 暂停
                if (s === 'playing' && btn === TTS.getCurrentBtn()) {
                    TTS.pause();
                    return;
                }
                // idle / 别的按钮在播 → 从头开始
                const text = b.getText();
                if (!text || !text.trim()) {
                    TTS.showTip('(暂无可朗读内容)');
                    return;
                }
                TTS.speak(text, { lang: b.lang, button: btn });
            });
        });
    }

    function escapeAttr(s) { return String(s || '').replace(/"/g, '&quot;'); }

    // ---------- Init ----------

    function init() {
        initTheme();

        // 报头日期
        document.getElementById('masthead-date').textContent = todayChinese();
        document.getElementById('footer-year').textContent = new Date().getFullYear();

        // 返回按钮
        const btnBack = document.getElementById('btn-back');
        if (btnBack) btnBack.addEventListener('click', () => navigate('/'));

        setupThemeToggle();
        setupLightbox();
        setupMobileNavToggle();
        setupTtsButtons();
        setupArticleNavigation();
        setupTocSheet();       // 底部目录弹层
        setupBackToTop();      // 回到顶部按钮
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
