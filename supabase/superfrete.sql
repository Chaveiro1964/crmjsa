-- JSA CRM: suporte a metadados de etiquetas SuperFrete
-- Aplicar APÓS o schema base supabase/schema.sql no SQL Editor do Supabase.
-- Segredos e dados pessoais completos não são armazenados nesta tabela.
begin;

create table if not exists public.shipping_labels (
  id text not null,
  organization_id uuid not null references public.organizations(id) on delete cascade,
  sale_id text,
  provider text not null default 'superfrete' check (provider in ('superfrete')),
  provider_order_id text not null,
  service_id integer,
  carrier text not null default '',
  status text not null default 'pending' check (status in ('pending','released','posted','delivered','cancelled','canceled','unknown')),
  amount numeric(14,2) check (amount is null or amount >= 0),
  origin_postal_code text not null default '' check (origin_postal_code = '' or origin_postal_code ~ '^[0-9]{8}$'),
  destination_postal_code text not null default '' check (destination_postal_code = '' or destination_postal_code ~ '^[0-9]{8}$'),
  tracking_code text not null default '',
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (id),
  unique (organization_id, id),
  unique (organization_id, provider, provider_order_id),
  foreign key (organization_id, sale_id)
    references public.sales(organization_id, id) on delete set null (sale_id)
);

create index if not exists shipping_labels_org_status_idx
  on public.shipping_labels(organization_id, status, created_at desc);
create index if not exists shipping_labels_org_tracking_idx
  on public.shipping_labels(organization_id, tracking_code) where tracking_code <> '';

alter table public.shipping_labels enable row level security;
drop policy if exists shipping_labels_select_member on public.shipping_labels;
drop policy if exists shipping_labels_insert_member on public.shipping_labels;
drop policy if exists shipping_labels_update_member on public.shipping_labels;
drop policy if exists shipping_labels_delete_admin on public.shipping_labels;
create policy shipping_labels_select_member on public.shipping_labels for select to authenticated
  using (public.is_org_member(organization_id));
create policy shipping_labels_insert_member on public.shipping_labels for insert to authenticated
  with check (public.is_org_member(organization_id));
create policy shipping_labels_update_member on public.shipping_labels for update to authenticated
  using (public.is_org_member(organization_id)) with check (public.is_org_member(organization_id));
create policy shipping_labels_delete_admin on public.shipping_labels for delete to authenticated
  using (public.is_org_admin(organization_id));

-- A tabela usa a função touch_updated_at criada pelo schema base.
drop trigger if exists touch_updated_at on public.shipping_labels;
create trigger touch_updated_at before update on public.shipping_labels
  for each row execute function public.touch_updated_at();

revoke all on public.shipping_labels from anon;
grant select, insert, update, delete on public.shipping_labels to authenticated;
comment on table public.shipping_labels is
  'Metadados de etiquetas de envio por organização; nunca armazena token SuperFrete nem CPF/endereço completo.';

commit;
