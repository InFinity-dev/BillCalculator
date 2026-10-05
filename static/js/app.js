/* Shared interactions and formatting helpers for the server-rendered pages. */
function getCsrfToken() { return document.querySelector('meta[name="csrf-token"]').content; }
function formatNumber(num) { return new Intl.NumberFormat('ko-KR').format(num); }
function escapeHtml(value) {
    const element = document.createElement('span');
    element.textContent = value == null ? '' : String(value);
    return element.innerHTML;
}
async function fetchAPI(url, method = 'GET', data = null) {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (data && method !== 'GET') {
        if (data instanceof FormData) {
            if (!data.has('_csrf_token')) data.set('_csrf_token', getCsrfToken());
            options.body = data; delete options.headers['Content-Type'];
        } else options.body = JSON.stringify({ ...data, _csrf_token: getCsrfToken() });
    }
    const response = await fetch(url, options);
    return response.json();
}
const modalTriggers = new Map();
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modalTriggers.set(modalId, document.activeElement);
    modal.classList.add('active');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    modal.querySelector('input:not([type=hidden]), select, textarea, button')?.focus();
}
function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.classList.remove('active');
    modal.setAttribute('aria-hidden', 'true');
    if (!document.querySelector('.modal.active')) document.body.classList.remove('modal-open');
    modalTriggers.get(modalId)?.focus();
    modalTriggers.delete(modalId);
}
const menuToggle = document.getElementById('menuToggle');
const navBackdrop = document.getElementById('navBackdrop');
function setNavigationOpen(open) {
    document.body.classList.toggle('nav-open', open);
    menuToggle.setAttribute('aria-expanded', String(open));
    menuToggle.setAttribute('aria-label', open ? '메뉴 닫기' : '메뉴 열기');
    navBackdrop.hidden = !open;
    document.getElementById('appNavigation').inert = !open && window.innerWidth <= 768;
    if (open) document.querySelector('.nav-link.active')?.focus();
    else menuToggle.focus();
}
menuToggle.addEventListener('click', () => setNavigationOpen(!document.body.classList.contains('nav-open')));
navBackdrop.addEventListener('click', () => setNavigationOpen(false));
window.matchMedia('(min-width: 769px)').addEventListener('change', event => {
    if (event.matches && document.body.classList.contains('nav-open')) setNavigationOpen(false);
    document.getElementById('appNavigation').inert = !event.matches && !document.body.classList.contains('nav-open');
});
document.getElementById('appNavigation').inert = window.innerWidth <= 768;
function selectTab(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const tabs = document.querySelector(`[aria-controls="${panelId}"]`)?.closest('.tabs');
    tabs?.querySelectorAll('.tab').forEach(tab => {
        const active = tab.getAttribute('aria-controls') === panelId;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        const target = document.getElementById(tab.getAttribute('aria-controls'));
        if (target) target.hidden = !active;
    });
}
document.addEventListener('click', event => {
    if (event.target.classList.contains('modal')) closeModal(event.target.id);
    if (event.target.closest('.tab')) requestAnimationFrame(enhancePage);
});
document.addEventListener('keydown', event => {
    const tab = event.target.closest('.tab');
    if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        const tabs = [...tab.closest('.tabs').querySelectorAll('.tab')];
        const index = tabs.indexOf(tab);
        const target = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
        event.preventDefault(); target.click(); target.focus();
    }
    const modal = document.querySelector('.modal.active');
    const navigationOpen = document.body.classList.contains('nav-open');
    if (event.key === 'Escape') {
        if (modal) closeModal(modal.id);
        else if (navigationOpen) setNavigationOpen(false);
    }
    const focusRoot = modal || (navigationOpen ? document.getElementById('appNavigation') : null);
    if (event.key !== 'Tab' || !focusRoot) return;
    const controls = [...focusRoot.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [tabindex="0"]')].filter(element => !element.disabled && element.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !focusRoot.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
let fieldSequence = 0;
function enhancePage() {
    document.querySelectorAll('.modal').forEach(modal => {
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        if (!modal.hasAttribute('aria-hidden')) modal.setAttribute('aria-hidden', 'true');
        const title = modal.querySelector('h2, h3');
        if (title) { if (!title.id) title.id = `${modal.id}-title`; modal.setAttribute('aria-labelledby', title.id); }
    });
    document.querySelectorAll('.tabs').forEach(tabs => {
        tabs.setAttribute('role', 'tablist');
        tabs.querySelectorAll('.tab').forEach(tab => {
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', String(tab.classList.contains('active')));
            tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
            const panelId = (tab.getAttribute('onclick') || '').match(/['"]([^'"]+)['"]/)?.[1];
            if (panelId) {
                tab.id ||= `${panelId}-button`;
                tab.setAttribute('aria-controls', panelId);
                const panel = document.getElementById(panelId);
                panel?.setAttribute('role', 'tabpanel');
                panel?.setAttribute('aria-labelledby', tab.id);
                if (panel) panel.hidden = !tab.classList.contains('active');
            }
        });
    });
    document.querySelectorAll('.form-group').forEach(group => {
        const label = group.querySelector('label');
        const control = group.querySelector('input:not([type=hidden]), select, textarea');
        if (label && control && !label.htmlFor) { control.id ||= `field-${++fieldSequence}`; label.htmlFor = control.id; }
    });
    document.querySelectorAll('table').forEach(table => {
        if (!table.closest('.table-scroll')) {
            const wrapper = document.createElement('div');
            wrapper.className = 'table-scroll';
            wrapper.tabIndex = 0;
            wrapper.setAttribute('role', 'region');
            wrapper.setAttribute('aria-label', '내역 표, 좌우로 스크롤하여 확인');
            table.before(wrapper); wrapper.append(table);
        }
        const headers = [...table.querySelectorAll('thead tr:last-child th')];
        headers.forEach(header => header.setAttribute('scope', 'col'));
        if (!headers.length) return;
        table.querySelectorAll('tbody tr').forEach(row => {
            [...row.children].forEach((cell, i) => {
                if (cell.tagName !== 'TD' || cell.colSpan > 1) return;
                const label = headers[i]?.textContent.trim() || '';
                if (cell.dataset.label !== label) cell.dataset.label = label;
                if (table.classList.contains('mobile-cards')) {
                    if (!cell.querySelector(':scope > .cell-value')) {
                        const value = document.createElement('div');
                        value.className = 'cell-value';
                        while (cell.firstChild) value.append(cell.firstChild);
                        cell.append(value);
                    }
                    cell.querySelectorAll('input:not([type=hidden]), select, textarea').forEach(input => {
                        if (!input.hasAttribute('aria-label')) input.setAttribute('aria-label', `${row.querySelector('td')?.textContent.trim() || '세대'} ${label}`.trim());
                    });
                }
            });
        });
    });
}
enhancePage();
let enhancementScheduled = false;
new MutationObserver(() => {
    if (enhancementScheduled) return;
    enhancementScheduled = true;
    requestAnimationFrame(() => { enhancementScheduled = false; enhancePage(); });
}).observe(document.body, { childList: true, subtree: true });
