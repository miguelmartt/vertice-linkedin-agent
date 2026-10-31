# vertice-linkedin-agent

[Read in English](README.md)

Agente de LinkedIn que vive en tu VPS como servicio de systemd. Busca novedades de IA y desarrollo, redacta un post con Claude Code y te lo manda a Telegram. Solo lo publica en tu perfil si pulsas «Publicar».

Lo hice para el LinkedIn de [VérticeDev](https://verticedev.es) y lo publico tal cual, con licencia MIT.

- **Busca** cada tres horas en cinco sitios: modelos en tendencia en Hugging Face, repos recientes que ganan estrellas en GitHub, la portada de Hacker News, los blogs de los laboratorios de IA (OpenAI, Google DeepMind, Google AI, Hugging Face y Mistral) y tus propios repos open source.
- **Redacta** lunes, miércoles y viernes a las 08:30 con Claude Code y tu suscripción, no con la API. Claude elige el tema entre los mejores de cada tipo, lee la página o el README y escribe el post en castellano, sin humo y sin inventar cifras.
- **Promociona tus repos** sin cansar: como mucho un post a la semana sobre un repo tuyo. Las versiones nuevas tienen prioridad, y `/repos` te deja elegir uno cuando quieras.
- **Te pregunta** por Telegram, con cuatro botones: Publicar, Cambios, Otro tema y Descartar. En «Cambios» puedes decirle qué cambiar o pegarle el texto ya corregido.
- **Publica** con la API oficial de LinkedIn (Posts API), con la tarjeta del enlace (título, descripción e imagen) y el texto escapado para que LinkedIn no lo corte.

Solo librería estándar de Python (3.9 o superior), SQLite y long polling de Telegram: sin dependencias y sin puertos abiertos.

## Cómo funciona

```
cada 3 h ───── buscar ─ Hugging Face · GitHub · Hacker News · blogs de IA · tus repos → temas puntuados
lun, mié, vie ─ redactar ─ lista corta (o un repo tuyo) → Claude elige y escribe → Telegram
              [Publicar] [Cambios] [Otro tema] [Descartar]
Publicar ───── imagen de la tarjeta → Posts API → enlace al post
cada día ───── avisos del permiso de LinkedIn (7 y 2 días antes) y limpieza de temas viejos
```

La puntuación solo ordena dentro de cada tipo: tendencia en Hugging Face, estrellas al día en GitHub y puntos en Hacker News. Lo que es de IA o de desarrollo pesa más, y lo que no es tech en Hacker News pesa menos. Claude recibe los tres mejores de cada tipo y los últimos posts publicados, para no repetirse.

## Comandos de Telegram

| Comando | Qué hace |
|---|---|
| `/borrador` | Prepara un post ahora |
| `/temas` | Lo que tiene para los próximos posts |
| `/repos` | Escribir sobre uno de tus repos |
| `/recoger` | Buscar novedades ahora |
| `/linkedin` | Conectar o renovar el permiso de LinkedIn |
| `/estado` | Permiso, próximo borrador, temas y uso de Claude |
| `/cancelar` | Olvida la respuesta que estaba esperando |

## Seguridad

- **Nada se publica sin ti.** Cada «Publicar» va ligado a la versión exacta del texto: el botón de un borrador anterior no publica el nuevo. Un doble clic no publica dos veces.
- **Solo tú** puedes usar el bot: tu usuario, en tu chat privado con él.
- **Todo lo de fuera es no fiable.** Las páginas, los README y los feeds se leen como datos, se recortan y nunca se ejecutan. Las URL que llegan de Hacker News o de un feed solo se piden si van a una IP pública, también tras cada redirección: un enlace no puede hacer que el agente pida algo a la red interna del VPS.
- **Claude no tiene herramientas**: sin acceso a archivos, a la red ni a comandos, en una carpeta vacía y solo con su token. Lo que escribe lo revisas tú antes de publicarlo.
- **Los secretos no salen.** El código de LinkedIn que pegas se borra del chat. Los tokens se tapan en los logs y en los errores. Claude no recibe los tokens del agente ni una clave de API.
- **Servicio aislado con systemd**: usuario propio, solo escribe en su carpeta de estado y no puede ejecutar nada de ella salvo Claude Code.

## Puesta en marcha

### 1. En tu ordenador

1. **Bot de Telegram.** Crea uno nuevo con @BotFather (no reutilices el de otro agente: Telegram solo deja a un proceso leer sus mensajes). Escríbele en privado y saca tu id de `https://api.telegram.org/bot<TOKEN>/getUpdates` (`message.chat.id`).
2. **Claude Code.** `claude setup-token` genera un token de larga duración de tu suscripción: es `CLAUDE_CODE_OAUTH_TOKEN`. Puedes usar el mismo que en otros agentes.
3. **App de LinkedIn.** En [LinkedIn Developers](https://www.linkedin.com/developers/apps), crea una app (te pide asociarla a una página de empresa tuya). En **Products**, añade «Share on LinkedIn» y «Sign In with LinkedIn using OpenID Connect». En **Auth**, añade la URL de vuelta: cualquier página https tuya, por ejemplo `https://tu-dominio.es/linkedin/callback`. Da igual que dé 404: solo copiarás su dirección. Apunta el Client ID y el Client Secret.

### 2. En el VPS (como root)

```bash
git clone https://github.com/miguelmartt/vertice-linkedin-agent.git /root/vertice-linkedin-agent
bash /root/vertice-linkedin-agent/deploy/install.sh
runuser -u vla -- env HOME=/var/lib/vertice-linkedin-agent bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
nano /etc/vertice-linkedin-agent/env      # tus datos y tokens
bash /root/vertice-linkedin-agent/deploy/install.sh   # comprueba todo y arranca
journalctl -u vertice-linkedin-agent -f
```

### 3. En Telegram

1. `/linkedin`, abre el enlace y acepta.
2. LinkedIn te lleva a tu URL de vuelta. Copia la dirección completa y pégasela al bot: borra tu mensaje y guarda el permiso.
3. `/borrador` para el primer post.

El permiso de LinkedIn dura 60 días y LinkedIn no da renovación automática a estas apps. El agente te avisa a 7 y a 2 días; renovarlo es repetir `/linkedin`.

Para actualizar: `git -C /root/vertice-linkedin-agent pull` y otra vez `install.sh`.

Para probar sin publicar, pon `VLA_DRY_RUN=1`: todo funciona igual, pero «Publicar» no llega a LinkedIn.

## Línea de órdenes

En el VPS, el instalador deja el atajo `vla`, que ejecuta como el usuario del servicio y con su configuración:

```bash
vla check            # configuración y conexiones (--claude: también una llamada real)
vla collect          # busca novedades y enseña los temas en consola, sin Claude ni Telegram
vla draft            # prepara un borrador y lo manda por Telegram
vla status           # estado
```

En tu ordenador, lo mismo con `python -m vla --env-file mi.env <orden>` desde la carpeta del repositorio.

## Límites conocidos

- La API de LinkedIn no genera la vista previa de los enlaces: el agente sube la imagen de la página (`og:image`). Si LinkedIn rechaza la tarjeta, publica el post sin ella y te lo dice.
- LinkedIn retira cada versión de su API al año. El agente avisa cuando la de `VLA_LINKEDIN_VERSION` tiene diez meses.
- Claude Code gasta del cupo de tu suscripción. Hay un tope diario de llamadas (`VLA_LLM_MAX_CALLS_PER_DAY`); un borrador es una llamada (dos si el primero no cumple las reglas).
- Sin token, la API de GitHub da 60 peticiones por hora: de sobra para una búsqueda cada tres horas con unos pocos repos propios.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Incluyen un recorrido completo con dobles de Telegram, Claude, LinkedIn y las fuentes: búsqueda, borrador, cambios, otro tema, publicación con tarjeta, tarjeta rechazada, token revocado y doble clic. También cubren los casos de seguridad: escape del texto de LinkedIn, permiso con `state` de un solo uso, usuarios ajenos, URL internas y secretos en logs y subprocesos.

## Licencia

[MIT](LICENSE) © 2026 VerticeDev
