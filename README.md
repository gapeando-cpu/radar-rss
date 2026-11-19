# Radar RSS

Vigila páginas web, blogs (canales RSS/Atom) e hilos de foro y envía las
publicaciones nuevas a Telegram, sin usar IA.

## Cómo funciona

- Cada hora, de 7:00 a 23:00 (hora de Madrid), un workflow de GitHub Actions revisa las fuentes de
  `fuentes.json` y avisa por Telegram solo de lo nuevo (sin duplicados).
- Un segundo horario de refuerzo (10 minutos después) cubre revisiones que
  GitHub se salte o retrase; si la anterior ya corrió, se salta sola.
- El estado (mensajes ya vistos) vive en `data/radar.db`, una base SQLite que
  el propio workflow guarda en el repo cuando hay novedades.
- Si una fuente falla, se reintenta hasta 3 veces con espera y el resto de
  fuentes sigue funcionando.

## Fuentes vigiladas

Se consultan con `/fuentes` desde el bot. Ahora mismo: cinco hilos de Rankia
del foro bancos-cajas (tarjetas/cashback, iGraal, BBVA 1.200€, ING 150€ y
cómo aumentar tus ingresos).

## Añadir o quitar fuentes

Edita `fuentes.json` y añade un bloque:

```json
{
  "id": "nombre-corto-sin-espacios",
  "nombre": "Nombre legible para los avisos",
  "tipo": "rankia_hilo | rss | pagina",
  "url": "https://..."
}
```

- `rankia_hilo`: hilo de foro de Rankia. Se lee la última página y se avisa de
  cada mensaje nuevo con autor, hora, texto y enlace directo.
- `rss`: canal RSS o Atom de un blog o web. Avisa de cada artículo nuevo.
- `pagina`: cualquier página web. Avisa de enlaces nuevos en su contenido.

La primera vez que se añade una fuente se siembra el historial: solo se
avisará de lo publicado a partir de ese momento (Rankia y RSS muestran los
dos últimos mensajes como aviso de estreno).

## Comandos en el bot

- `/fuentes`: lista las fuentes vigiladas.
- `/estado`: cuándo fue la última revisión y cómo fue.

## Configuración técnica

- Secretos del repo: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
- Sin dependencias: Python 3.12 estándar.
- Gratis: repo público, Actions no consume minutos de pago. GitHub puede
  retrasar ejecuciones programadas unos minutos; por eso existe el refuerzo.
