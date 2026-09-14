--
-- PostgreSQL database dump
--

\restrict bBdCtSkPPBYVFBgu23ZJWzPsOF8BK2xdyjGECrQApVu4namBGD8RI2Y0LjwElIY

-- Dumped from database version 15.17 (Debian 15.17-1.pgdg11+1)
-- Dumped by pg_dump version 18.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;


--
-- Name: EXTENSION citext; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION citext IS 'data type for case-insensitive character strings';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    product_slug text NOT NULL,
    template_id uuid,
    display_name text NOT NULL,
    system_prompt text NOT NULL,
    model text NOT NULL,
    tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    current_version_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_instance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_instance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    display_name text NOT NULL,
    whatsapp_number text,
    handoff_whatsapp text,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_template (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    display_name text NOT NULL,
    product_slug text NOT NULL,
    system_prompt text NOT NULL,
    default_model text NOT NULL,
    default_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    default_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    version_number integer NOT NULL,
    system_prompt text NOT NULL,
    model text NOT NULL,
    tools jsonb NOT NULL,
    config jsonb NOT NULL,
    change_summary text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_chat_histories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_chat_histories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    thread_id text NOT NULL,
    session_id text NOT NULL,
    message jsonb NOT NULL,
    message_order bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_chat_histories_message_order_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_chat_histories_message_order_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_chat_histories_message_order_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_chat_histories_message_order_seq OWNED BY public.ai_chat_histories.message_order;


--
-- Name: app_chat_histories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_chat_histories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    session_id text NOT NULL,
    sender text NOT NULL,
    message text NOT NULL,
    message_time timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: blacklist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.blacklist (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    numero text NOT NULL,
    estado boolean DEFAULT true NOT NULL,
    fecha_creacion timestamp with time zone DEFAULT now() NOT NULL,
    fecha_actualizacion timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: handoff_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.handoff_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    thread_id text NOT NULL,
    reason text NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kb_chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_chunk (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    content text NOT NULL,
    embedding public.vector(1536) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: product; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product (
    slug text NOT NULL,
    display_name text NOT NULL,
    domain text,
    domain_db_name text,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_chat_histories message_order; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_histories ALTER COLUMN message_order SET DEFAULT nextval('public.ai_chat_histories_message_order_seq'::regclass);


--
-- Name: agent_instance agent_instance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instance
    ADD CONSTRAINT agent_instance_pkey PRIMARY KEY (id);


--
-- Name: agent_instance agent_instance_whatsapp_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instance
    ADD CONSTRAINT agent_instance_whatsapp_number_key UNIQUE (whatsapp_number);


--
-- Name: agent agent_organization_id_product_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT agent_organization_id_product_slug_key UNIQUE (organization_id, product_slug);


--
-- Name: agent agent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT agent_pkey PRIMARY KEY (id);


--
-- Name: agent_template agent_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_template
    ADD CONSTRAINT agent_template_pkey PRIMARY KEY (id);


--
-- Name: agent_template agent_template_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_template
    ADD CONSTRAINT agent_template_slug_key UNIQUE (slug);


--
-- Name: agent_version agent_version_agent_id_version_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_version
    ADD CONSTRAINT agent_version_agent_id_version_number_key UNIQUE (agent_id, version_number);


--
-- Name: agent_version agent_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_version
    ADD CONSTRAINT agent_version_pkey PRIMARY KEY (id);


--
-- Name: ai_chat_histories ai_chat_histories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_histories
    ADD CONSTRAINT ai_chat_histories_pkey PRIMARY KEY (id);


--
-- Name: app_chat_histories app_chat_histories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_chat_histories
    ADD CONSTRAINT app_chat_histories_pkey PRIMARY KEY (id);


--
-- Name: blacklist blacklist_organization_id_numero_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blacklist
    ADD CONSTRAINT blacklist_organization_id_numero_key UNIQUE (organization_id, numero);


--
-- Name: blacklist blacklist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blacklist
    ADD CONSTRAINT blacklist_pkey PRIMARY KEY (id);


--
-- Name: handoff_event handoff_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_event
    ADD CONSTRAINT handoff_event_pkey PRIMARY KEY (id);


--
-- Name: kb_chunk kb_chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunk
    ADD CONSTRAINT kb_chunk_pkey PRIMARY KEY (id);


--
-- Name: product product_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product
    ADD CONSTRAINT product_pkey PRIMARY KEY (slug);


--
-- Name: agent_instance_agent_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_instance_agent_id_idx ON public.agent_instance USING btree (agent_id);


--
-- Name: blacklist_active_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX blacklist_active_lookup_idx ON public.blacklist USING btree (organization_id, numero) WHERE (estado = true);


--
-- Name: idx_agent_organization; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_organization ON public.agent USING btree (organization_id);


--
-- Name: idx_agent_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_product ON public.agent USING btree (product_slug);


--
-- Name: idx_agent_template_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_template_product ON public.agent_template USING btree (product_slug);


--
-- Name: idx_agent_version_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_version_agent ON public.agent_version USING btree (agent_id);


--
-- Name: idx_ai_history_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_agent ON public.ai_chat_histories USING btree (agent_id);


--
-- Name: idx_ai_history_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_created_at ON public.ai_chat_histories USING btree (created_at DESC);


--
-- Name: idx_ai_history_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_org ON public.ai_chat_histories USING btree (organization_id);


--
-- Name: idx_ai_history_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_session ON public.ai_chat_histories USING btree (session_id);


--
-- Name: idx_ai_history_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_thread ON public.ai_chat_histories USING btree (thread_id);


--
-- Name: idx_ai_history_thread_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_history_thread_order ON public.ai_chat_histories USING btree (thread_id, message_order);


--
-- Name: idx_app_history_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_app_history_agent ON public.app_chat_histories USING btree (agent_id);


--
-- Name: idx_app_history_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_app_history_session ON public.app_chat_histories USING btree (session_id);


--
-- Name: idx_app_history_session_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_app_history_session_time ON public.app_chat_histories USING btree (session_id, message_time);


--
-- Name: idx_handoff_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoff_agent ON public.handoff_event USING btree (agent_id);


--
-- Name: idx_handoff_reason; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoff_reason ON public.handoff_event USING btree (reason);


--
-- Name: idx_handoff_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoff_thread ON public.handoff_event USING btree (thread_id);


--
-- Name: idx_handoff_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoff_unresolved ON public.handoff_event USING btree (agent_id, created_at DESC) WHERE (resolved_at IS NULL);


--
-- Name: kb_chunk_embedding_hnsw_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunk_embedding_hnsw_idx ON public.kb_chunk USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: kb_chunk_instance_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX kb_chunk_instance_id_idx ON public.kb_chunk USING btree (instance_id);


--
-- Name: agent_template trg_agent_template_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_template_updated BEFORE UPDATE ON public.agent_template FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: agent trg_agent_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_updated BEFORE UPDATE ON public.agent FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: agent_instance agent_instance_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instance
    ADD CONSTRAINT agent_instance_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE CASCADE;


--
-- Name: agent agent_product_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT agent_product_slug_fkey FOREIGN KEY (product_slug) REFERENCES public.product(slug);


--
-- Name: agent agent_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT agent_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.agent_template(id) ON DELETE SET NULL;


--
-- Name: agent_template agent_template_product_slug_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_template
    ADD CONSTRAINT agent_template_product_slug_fkey FOREIGN KEY (product_slug) REFERENCES public.product(slug);


--
-- Name: agent_version agent_version_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_version
    ADD CONSTRAINT agent_version_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE CASCADE;


--
-- Name: ai_chat_histories ai_chat_histories_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_histories
    ADD CONSTRAINT ai_chat_histories_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE CASCADE;


--
-- Name: app_chat_histories app_chat_histories_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_chat_histories
    ADD CONSTRAINT app_chat_histories_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE CASCADE;


--
-- Name: agent fk_agent_current_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent
    ADD CONSTRAINT fk_agent_current_version FOREIGN KEY (current_version_id) REFERENCES public.agent_version(id) ON DELETE SET NULL;


--
-- Name: handoff_event handoff_event_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_event
    ADD CONSTRAINT handoff_event_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent(id) ON DELETE CASCADE;


--
-- Name: kb_chunk kb_chunk_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunk
    ADD CONSTRAINT kb_chunk_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.agent_instance(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict bBdCtSkPPBYVFBgu23ZJWzPsOF8BK2xdyjGECrQApVu4namBGD8RI2Y0LjwElIY

