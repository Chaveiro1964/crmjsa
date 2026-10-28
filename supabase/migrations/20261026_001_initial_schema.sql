-- JSA CRM / Supabase — migração inicial versionada.
-- Execute em projeto Supabase novo pelo SQL Editor ou `supabase db push`.
-- A aplicação não deve usar service_role no navegador. Preserve legacy_kv_store
-- até validar todos os dados migrados e os consumidores dos payloads históricos.

begin;

create extension if not exists pgcrypto;

create table if not exists public.organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(trim(name)) > 0),
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.organization_members (
  organization_id uuid not null references public.organizations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'member' check (role in ('owner','admin','member')),
  created_at timestamptz not null default now(),
  primary key (organization_id, user_id)
);
create index if not exists organization_members_user_idx on public.organization_members(user_id, organization_id);

create or replace function public.is_org_member(p_org_id uuid)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
  select exists (
    select 1 from public.organization_members m
    where m.organization_id = p_org_id and m.user_id = (select auth.uid())
  );
$$;

create or replace function public.is_org_admin(p_org_id uuid)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
  select exists (
    select 1 from public.organization_members m
    where m.organization_id = p_org_id and m.user_id = (select auth.uid())
      and m.role in ('owner','admin')
  );
$$;

create or replace function public.is_org_creator(p_org_id uuid)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
  select exists (
    select 1 from public.organizations o
    where o.id = p_org_id and o.created_by = (select auth.uid())
  );
$$;

revoke all on function public.is_org_member(uuid) from public, anon;
revoke all on function public.is_org_admin(uuid) from public, anon;
revoke all on function public.is_org_creator(uuid) from public, anon;
grant execute on function public.is_org_member(uuid) to authenticated;
grant execute on function public.is_org_admin(uuid) to authenticated;
grant execute on function public.is_org_creator(uuid) to authenticated;

create table if not exists public.clients (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  name text not null default '',
  phone text not null default '',
  address text not null default '',
  document text not null default '',
  city text not null default '',
  state text not null default '',
  postal_code text not null default '',
  source text not null default '',
  stage text not null default '',
  tags jsonb not null default '[]'::jsonb check (jsonb_typeof(tags) = 'array'),
  next_return jsonb,
  source_payload jsonb not null default '{}'::jsonb check (jsonb_typeof(source_payload) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id)
);
create index if not exists clients_org_name_idx on public.clients(organization_id, lower(name));
create index if not exists clients_org_phone_idx on public.clients(organization_id, phone);

create table if not exists public.products (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  code text not null default '',
  name text not null default '',
  description text not null default '',
  cost_price numeric(14,4) not null default 0 check (cost_price >= 0),
  sale_price numeric(14,4) not null default 0 check (sale_price >= 0),
  active boolean not null default true,
  stock numeric(14,3),
  minimum_stock numeric(14,3) not null default 0 check (minimum_stock >= 0),
  source_payload jsonb not null default '{}'::jsonb check (jsonb_typeof(source_payload) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id)
);
create unique index if not exists products_org_code_unique on public.products(organization_id, code) where code <> '';
create index if not exists products_org_name_idx on public.products(organization_id, lower(name));

create table if not exists public.sales (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  client_id text,
  client_name_snapshot text not null default '',
  sale_date date,
  status text not null default '',
  shipping_amount numeric(14,2) not null default 0 check (shipping_amount >= 0),
  shipping_paid_by text not null default '',
  payment_method text not null default '',
  installments integer check (installments is null or installments > 0),
  total_amount numeric(14,2) not null default 0 check (total_amount >= 0),
  source_payload jsonb not null default '{}'::jsonb check (jsonb_typeof(source_payload) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id),
  constraint sales_client_org_fk foreign key (organization_id, client_id)
    references public.clients(organization_id, id) on delete set null (client_id)
);
create index if not exists sales_org_date_idx on public.sales(organization_id, sale_date desc);
create index if not exists sales_org_client_idx on public.sales(organization_id, client_id);

create table if not exists public.sale_items (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  sale_id text not null,
  line_no integer not null check (line_no > 0),
  product_id text,
  product_name_snapshot text not null default '',
  model text not null default '',
  quantity numeric(14,3) not null check (quantity > 0),
  unit_cost numeric(14,4) not null default 0 check (unit_cost >= 0),
  unit_price numeric(14,4) not null default 0 check (unit_price >= 0),
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (organization_id, sale_id, line_no),
  foreign key (organization_id, sale_id) references public.sales(organization_id, id) on delete cascade,
  foreign key (organization_id, product_id) references public.products(organization_id, id) on delete set null (product_id)
);

create table if not exists public.quotes (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  client_id text,
  client_name_snapshot text not null default '',
  quote_date date,
  postal_code text not null default '',
  note text not null default '',
  shipping_amount numeric(14,2) not null default 0 check (shipping_amount >= 0),
  shipping_paid_by text not null default '',
  payment_method text not null default '',
  installments integer check (installments is null or installments > 0),
  total_amount numeric(14,2) not null default 0 check (total_amount >= 0),
  status text not null default '',
  loss_reason text not null default '',
  source_payload jsonb not null default '{}'::jsonb check (jsonb_typeof(source_payload) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id),
  constraint quotes_client_org_fk foreign key (organization_id, client_id)
    references public.clients(organization_id, id) on delete set null (client_id)
);
create index if not exists quotes_org_date_idx on public.quotes(organization_id, quote_date desc);
create index if not exists quotes_org_status_idx on public.quotes(organization_id, status);

create table if not exists public.quote_items (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  quote_id text not null,
  line_no integer not null check (line_no > 0),
  product_id text,
  product_name_snapshot text not null default '',
  model text not null default '',
  quantity numeric(14,3) not null check (quantity > 0),
  unit_cost numeric(14,4) not null default 0 check (unit_cost >= 0),
  unit_price numeric(14,4) not null default 0 check (unit_price >= 0),
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (organization_id, quote_id, line_no),
  foreign key (organization_id, quote_id) references public.quotes(organization_id, id) on delete cascade,
  foreign key (organization_id, product_id) references public.products(organization_id, id) on delete set null (product_id)
);

create table if not exists public.payable_accounts (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  supplier text not null default '',
  category text not null default '',
  amount numeric(14,2) not null default 0 check (amount >= 0),
  due_date date,
  status text not null default '',
  note text not null default '',
  paid_at timestamptz,
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id)
);
create index if not exists payable_accounts_due_idx on public.payable_accounts(organization_id, due_date, status);

create table if not exists public.marketing_expenses (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  description text not null default '',
  amount numeric(14,2) not null default 0 check (amount >= 0),
  expense_date date,
  channel text not null default '',
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id)
);
create index if not exists marketing_expenses_date_idx on public.marketing_expenses(organization_id, expense_date desc);

create table if not exists public.activities (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  client_id text,
  activity_type text not null default '',
  description text not null default '',
  happened_at timestamptz,
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id),
  foreign key (organization_id, client_id) references public.clients(organization_id, id) on delete set null (client_id)
);
create index if not exists activities_org_time_idx on public.activities(organization_id, happened_at desc);

create table if not exists public.missing_codes (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  code text not null default '',
  occurrences integer not null default 0 check (occurrences >= 0),
  first_seen date,
  last_seen date,
  source_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id), unique (organization_id, id)
);
create index if not exists missing_codes_org_code_idx on public.missing_codes(organization_id, code);

-- Conserva configurações e módulos cuja estrutura varia ou ainda não tem dados
-- observáveis no SQLite de origem (marketing, agenda, SuperFrete, templates etc.).
create table if not exists public.app_state (
  organization_id uuid not null references public.organizations(id) on delete cascade,
  key text not null check (length(key) between 1 and 128),
  value jsonb not null,
  source_updated_at text,
  updated_at timestamptz not null default now(),
  primary key (organization_id, key)
);

-- Staging/auditoria reversível: guarda o texto JSON original e a forma JSONB.
-- Não use esta tabela como substituto das entidades normalizadas.
create table if not exists public.legacy_kv_store (
  organization_id uuid not null references public.organizations(id) on delete cascade,
  key text not null,
  value_raw text not null,
  payload jsonb not null,
  updated_at_raw text not null,
  migrated_at timestamptz not null default now(),
  primary key (organization_id, key)
);

create table if not exists public.migration_runs (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  source_name text not null,
  source_sha256 text not null,
  report jsonb not null default '{}'::jsonb,
  status text not null check (status in ('dry_run','completed','failed')),
  started_at timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists migration_runs_org_time_idx on public.migration_runs(organization_id, started_at desc);

create or replace function public.touch_updated_at()
returns trigger language plpgsql set search_path = public, pg_temp as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- Only tables with updated_at are attached.
do $$
declare t text;
begin
  foreach t in array array['organizations','clients','products','sales','quotes','payable_accounts','marketing_expenses','missing_codes','app_state'] loop
    execute format('drop trigger if exists touch_updated_at on public.%I', t);
    execute format('create trigger touch_updated_at before update on public.%I for each row execute function public.touch_updated_at()', t);
  end loop;
end $$;

-- RLS is deny-by-default for anon. Members can read/write only within their org;
-- destructive delete requires an owner/admin role.
alter table public.organizations enable row level security;
alter table public.organization_members enable row level security;
alter table public.clients enable row level security;
alter table public.products enable row level security;
alter table public.sales enable row level security;
alter table public.sale_items enable row level security;
alter table public.quotes enable row level security;
alter table public.quote_items enable row level security;
alter table public.payable_accounts enable row level security;
alter table public.marketing_expenses enable row level security;
alter table public.activities enable row level security;
alter table public.missing_codes enable row level security;
alter table public.app_state enable row level security;
alter table public.legacy_kv_store enable row level security;
alter table public.migration_runs enable row level security;

drop policy if exists organizations_select_member on public.organizations;
drop policy if exists organizations_insert_creator on public.organizations;
drop policy if exists organizations_update_admin on public.organizations;
drop policy if exists organizations_delete_owner on public.organizations;
drop policy if exists members_select_self_or_org on public.organization_members;
drop policy if exists members_insert_creator_or_admin on public.organization_members;
drop policy if exists members_update_admin on public.organization_members;
drop policy if exists members_delete_admin on public.organization_members;
drop policy if exists migration_runs_select_admin on public.migration_runs;
drop policy if exists migration_runs_insert_admin on public.migration_runs;
drop policy if exists migration_runs_update_admin on public.migration_runs;
drop policy if exists migration_runs_delete_owner on public.migration_runs;

create policy organizations_select_member on public.organizations for select to authenticated
  using (public.is_org_member(id));
create policy organizations_insert_creator on public.organizations for insert to authenticated
  with check (created_by = (select auth.uid()));
create policy organizations_update_admin on public.organizations for update to authenticated
  using (public.is_org_admin(id)) with check (public.is_org_admin(id));
create policy organizations_delete_owner on public.organizations for delete to authenticated
  using (exists (select 1 from public.organization_members m where m.organization_id = id and m.user_id = (select auth.uid()) and m.role = 'owner'));

create policy members_select_self_or_org on public.organization_members for select to authenticated
  using (user_id = (select auth.uid()) or public.is_org_member(organization_id));
create policy members_insert_creator_or_admin on public.organization_members for insert to authenticated
  with check (public.is_org_admin(organization_id) or (user_id = (select auth.uid()) and public.is_org_creator(organization_id) and role = 'owner'));
create policy members_update_admin on public.organization_members for update to authenticated
  using (public.is_org_admin(organization_id)) with check (public.is_org_admin(organization_id));
create policy members_delete_admin on public.organization_members for delete to authenticated
  using (public.is_org_admin(organization_id));

-- A standard member may perform CRUD within their own organization; only an
-- administrator can delete. Helpers are SECURITY DEFINER to avoid recursive RLS.
do $$
declare t text;
begin
  foreach t in array array['clients','products','sales','sale_items','quotes','quote_items','payable_accounts','marketing_expenses','activities','missing_codes','app_state','legacy_kv_store'] loop
    execute format('drop policy if exists %I on public.%I', t || '_select_member', t);
    execute format('drop policy if exists %I on public.%I', t || '_insert_member', t);
    execute format('drop policy if exists %I on public.%I', t || '_update_member', t);
    execute format('drop policy if exists %I on public.%I', t || '_delete_admin', t);
    execute format('create policy %I on public.%I for select to authenticated using (public.is_org_member(organization_id))', t || '_select_member', t);
    execute format('create policy %I on public.%I for insert to authenticated with check (public.is_org_member(organization_id))', t || '_insert_member', t);
    execute format('create policy %I on public.%I for update to authenticated using (public.is_org_member(organization_id)) with check (public.is_org_member(organization_id))', t || '_update_member', t);
    execute format('create policy %I on public.%I for delete to authenticated using (public.is_org_admin(organization_id))', t || '_delete_admin', t);
  end loop;
end $$;

create policy migration_runs_select_admin on public.migration_runs for select to authenticated
  using (public.is_org_admin(organization_id));
create policy migration_runs_insert_admin on public.migration_runs for insert to authenticated
  with check (public.is_org_admin(organization_id));
create policy migration_runs_update_admin on public.migration_runs for update to authenticated
  using (public.is_org_admin(organization_id)) with check (public.is_org_admin(organization_id));
create policy migration_runs_delete_owner on public.migration_runs for delete to authenticated
  using (exists (select 1 from public.organization_members m where m.organization_id = migration_runs.organization_id and m.user_id = (select auth.uid()) and m.role = 'owner'));

-- No anonymous table access. Supabase grants authenticated CRUD but RLS is the
-- actual authorization boundary. Keep service_role/server secrets server-side.
revoke all on all tables in schema public from anon;
grant select, insert, update, delete on
  public.organizations, public.organization_members, public.clients, public.products,
  public.sales, public.sale_items, public.quotes, public.quote_items,
  public.payable_accounts, public.marketing_expenses, public.activities,
  public.missing_codes, public.app_state, public.legacy_kv_store, public.migration_runs
  to authenticated;

comment on table public.legacy_kv_store is 'Auditoria reversível de dados JSON legados. Dados comerciais devem usar tabelas normalizadas.';
comment on table public.app_state is 'Configurações e módulos sem schema estável observável; não contém clientes, vendas, produtos ou orçamentos.';
comment on column public.quotes.client_name_snapshot is 'Snapshot original preservado mesmo quando cliente_id é resolvido.';
comment on column public.quote_items.product_name_snapshot is 'Snapshot histórico do nome do produto no orçamento.';

commit;
