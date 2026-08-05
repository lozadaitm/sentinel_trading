-- ==================================================================
-- 003_disable_m15_grinder.sql  |  Apagar el grinder embebido en el Sentinel
-- ==================================================================
-- APLICAR SOLO cuando la instancia M5 este lista para arrancar.
-- Es un flag de RUNTIME: revertir es otro UPDATE, no un despliegue.
--
-- QUE HACE
--   El Sentinel M15 lleva dentro un grinder M5 degradado que solo se activa
--   con la cesta en core>=4. Se sustituye por el motor M5 independiente
--   (bot/strategy_m5.py), que es el port fiel del EA SmartCut v13.02.
--
-- CONSECUENCIA ESPERADA (no es un efecto lateral: es el objetivo)
--   El presupuesto del Smart Healer BAJA. Sus amputaciones se financiaban con
--   los deals ganadores del grinder, porque compartian magic:
--       _check_healing() filtra por d.magic == self.b.magic
--   Con el M5 en su propio magic, esos verdes dejan de alimentar al Healer.
--   Dado docs/memory/healer-unwind-hedge-cascade.md (Healer + Unwind
--   desarmando el hedge = los dos blowups), esto es DE-RISKING DELIBERADO.
--
-- QUE MONITORIZAR DURANTE 2 SEMANAS
--   1. Amputaciones por semana:
--        SELECT date_trunc('week', created_at) AS semana, count(*)
--          FROM bot_logs
--         WHERE user_id = '<USER_UUID>' AND bot_id = 'm15' AND log_type = 'HEALER'
--         GROUP BY 1 ORDER BY 1 DESC;
--   2. P&L por ciclo (log_type = 'EXITO').
--   3. Profundidad maxima de cesta (posiciones core simultaneas).
--
-- LIMPIEZA DE CODIGO
--   Solo DESPUES de confirmar estabilidad: borrar _run_grinder,
--   _grinder_trailing y _is_grinder de bot/strategy.py, junto con sus
--   exclusiones en _net_exposure, _apply_healing, _check_rescue y core_count.
--   Eso elimina de paso la identidad fragil por comment con fallback al
--   lote 0.05 (cualquier posicion de ese tamaño se disfrazaba de grinder).
-- ==================================================================

UPDATE public.bot_config
   SET use_grinder = false,
       updated_at  = NOW()
 WHERE user_id = '<USER_UUID>'
   AND bot_id  = 'm15';

-- Reversion:
-- UPDATE public.bot_config
--    SET use_grinder = true, updated_at = NOW()
--  WHERE user_id = '<USER_UUID>' AND bot_id = 'm15';
