# Página inicial editável Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Administradores com `manage_homepage` publicam banners e frase da homepage pelo painel, com fallback visual quando não há banners cadastrados.

**Architecture:** Django persiste uma configuração singleton e banners ordenados no app `products`, expondo uma leitura pública e endpoints administrativos protegidos. React busca a configuração ao abrir a loja e usa os assets atuais como fallback; a tela admin edita cada banner e salva a ordem em operação separada.

**Tech Stack:** Django REST Framework, Pillow, armazenamento Django/Cloudinary, React 19, Vite, Vitest, Testing Library.

**Spec:** `../specs/2026-11-09-pagina-inicial-design.md`

## Global Constraints

- Imagens JPEG, PNG ou WebP com até 5 MB por arquivo, verificadas por conteúdo.
- Links aceitam somente caminhos internos iniciados por `/`, sem `//` inicial.
- A faixa conserva fundo preto e texto branco; sem agendamento e sem editar outras seções.
- A API administrativa exige `manage_homepage`; sem autenticação retorna 401, sem permissão retorna 403.
- Não adicionar dependências se Pillow e o storage existente forem suficientes.

## Review Focus

- Arquivo com extensão aceita mas bytes inválidos: 400 com campo e mensagem; coberto na tarefa 2.
- Ordem com IDs repetidos ou omitidos: 400 sem escrita parcial; coberto na tarefa 2.
- Link `//host` ou `javascript:`: 400 e nenhum link inseguro renderizado; coberto nas tarefas 2 e 3.
- Troca ou remoção de imagem: arquivo anterior removido apenas após persistência bem-sucedida; coberto na tarefa 2.
- Admin que tinha permissões limitadas antes da migração: continua sem `manage_homepage`; coberto na tarefa 1.

---

### Task 1: Persistência e permissão

**Files:**
- Modify: `backend/authentication/admin_permissions.py`, `backend/authentication/permissions.py`
- Create: `backend/authentication/migrations/0006_manage_homepage.py`
- Modify: `backend/products/models.py`
- Create: `backend/products/migrations/0015_homepage.py`
- Test: `backend/authentication/test_homepage_permission.py`, `backend/products/test_homepage_models.py`

**Interfaces:**
- Produces `CanManageHomepage` e código `manage_homepage`.
- Produces `HomepageSettings` (singleton `pk=1`, `tagline`) e `HomepageBanner` (`id`, `image_desktop`, `image_mobile`, `alt_text`, `link`, `display_order`).
- Constante de frase padrão: `Deus primeiro. Negócios depois.`.

- [ ] **Step 1: Write failing tests** for default tagline, banner ordering, new permission in serialized definitions, full-access legacy admins receiving the new permission, and limited admins not receiving it.
- [ ] **Step 2: Run tests to verify failure:** `cd backend && DJANGO_SETTINGS_MODULE=core.settings.test python -m pytest authentication/test_homepage_permission.py products/test_homepage_models.py -q`.
- [ ] **Step 3: Add models, migrations, permission class and permission definition.** The auth data migration grants the new code only to admins holding all six previous managed permissions, including accounts migrated with full access; superusers already bypass explicit grants.
- [ ] **Step 4: Run the same tests and `python manage.py makemigrations --check --dry-run --settings=core.settings.test`; expect pass and no pending migration.**
- [ ] **Step 5: Commit backend task:** `feat: add homepage models and permission`.

### Task 2: API pública e administrativa

**Files:**
- Create: `backend/products/homepage_serializers.py`, `backend/products/homepage_views.py`, `backend/products/test_homepage_api.py`
- Modify: `backend/products/urls.py`

**Interfaces:**
- `GET /api/catalog/homepage/` → `{tagline: string, banners: HomepageBanner[]}`; public, ordered, `Cache-Control: no-store`.
- `GET /api/catalog/homepage/admin/` → same shape with persisted banners only; protected.
- `PATCH /api/catalog/homepage/tagline/` with `{tagline: string}`; protected.
- `POST /api/catalog/homepage/banners/` and `PATCH /api/catalog/homepage/banners/<uuid:id>/` accept multipart with `image_desktop`, optional `image_mobile`, `alt_text`, optional `link`; PATCH also accepts `remove_image_mobile=true` to clear the optional image. Protected.
- `DELETE /api/catalog/homepage/banners/<uuid:id>/`; protected.
- `PUT /api/catalog/homepage/banners/order/` with `{ids: string[]}`; protected and atomic.
- Banner response keys: `id`, `image_desktop`, `image_mobile`, `alt_text`, `link`, `display_order`.

- [ ] **Step 1: Write failing API tests** for public empty/configured read and no-store header, all admin routes returning 401/403, CRUD and ordered read, phrase update, clearing mobile image/link, invalid or oversized bytes, invalid link, invalid order, and image replacement/deletion cleanup. Use `captureOnCommitCallbacks(execute=True)` for storage cleanup assertions.
- [ ] **Step 2: Run `cd backend && DJANGO_SETTINGS_MODULE=core.settings.test python -m pytest products/test_homepage_api.py -q`; expect failures in new endpoints.**
- [ ] **Step 3: Implement serializers and views.** Use Pillow `Image.verify()` plus format allowlist and 5 MB validation; reset file pointer before storage. Validate complete ID set before a transaction. Return field-specific 400 details matching existing API error style. Delete old files with `transaction.on_commit` after successful mutation.
- [ ] **Step 4: Run API tests and `python manage.py check --settings=core.settings.test`; expect pass.**
- [ ] **Step 5: Commit backend task:** `feat: expose homepage management API`.

### Task 3: Loja lê banners e frase

**Files:**
- Modify: `frontend/src/pages/public/HomePage/index.jsx`
- Test: `frontend/src/pages/public/HomePage/index.test.jsx`

**Interfaces:**
- Consumes `GET /api/catalog/homepage/` from task 2.
- Fallback uses `/images/shio/hero-slide-1.png`, `/images/shio/hero-slide-2.jpg.png` and the current phrase.

- [ ] **Step 1: Write failing component tests** for configured banners and phrase, internal link click target, mobile `<picture>` source, empty list fallback, failed request fallback, and updated data after remount.
- [ ] **Step 2: Run `cd frontend && npm test -- --run src/pages/public/HomePage/index.test.jsx`; expect failures.**
- [ ] **Step 3: Fetch homepage config on mount and pass slides to the carousel.** Render `Link` only for valid internal paths, use `<picture>` with mobile source when provided, preserve current slide behavior and reset index when slide count changes. Render fallback immediately until config loads.
- [ ] **Step 4: Run the same tests; expect pass.**
- [ ] **Step 5: Commit frontend task:** `feat: render configured homepage content`.

### Task 4: Tela Página inicial e controles de acesso

**Files:**
- Create: `frontend/src/pages/admin/HomepagePage/index.jsx`, `frontend/src/pages/admin/HomepagePage/index.test.jsx`
- Modify: `frontend/src/lib/adminPermissions.js`, `frontend/src/routes/index.jsx`, `frontend/src/components/layout/admin/AdminNavbar.jsx`, `frontend/src/components/layout/admin/AdminNavbar.test.jsx`, `frontend/src/components/ProtectedRoute.test.jsx`

**Interfaces:**
- Consumes every admin endpoint from task 2.
- Adds `ADMIN_PERMISSIONS.HOMEPAGE = 'manage_homepage'` and route `/admin/homepage`.

- [ ] **Step 1: Write failing tests** for hidden navigation and blocked route without permission, initial data, separate banner create/edit/delete, mobile image/link removal, up/down reorder request, phrase update, client-side file size/type feedback, server error feedback and success state.
- [ ] **Step 2: Run `cd frontend && npm test -- --run src/pages/admin/HomepagePage/index.test.jsx src/components/layout/admin/AdminNavbar.test.jsx src/components/ProtectedRoute.test.jsx`; expect failures.**
- [ ] **Step 3: Implement the route, nav entry and screen.** Use file inputs with `accept` for JPEG/PNG/WebP; validate 5 MB and format client-side for quick feedback while treating server validation as authoritative. Use up/down controls for keyboard accessibility. Keep mobile navigation usable with horizontal overflow as the new item is added. Each successful operation refreshes admin state; show clear success/error text. Auth headers use `getAccessToken()`.
- [ ] **Step 4: Run targeted tests, `npm run build` and `npm run lint`; expect pass.**
- [ ] **Step 5: Commit frontend task:** `feat: manage homepage from admin panel`.

### Task 5: Integração final

**Files:**
- Modify only files needed to fix failures found by verification.

**Interfaces:**
- Complete backend/frontend contract above.

- [ ] **Step 1: Run `cd backend && DJANGO_SETTINGS_MODULE=core.settings.test python -m pytest authentication/test_homepage_permission.py products/test_homepage_models.py products/test_homepage_api.py -q`.**
- [ ] **Step 2: Run `cd frontend && npm test -- --run src/pages/public/HomePage/index.test.jsx src/pages/admin/HomepagePage/index.test.jsx src/components/layout/admin/AdminNavbar.test.jsx src/components/ProtectedRoute.test.jsx`.**
- [ ] **Step 3: Review actual API payload against frontend use and verify migration status, lint/build outputs, and both git worktrees are clean except intended changes.**
- [ ] **Step 4: Commit only fixes resulting from these checks; report tests and any deployment migration requirement.**
