# GSD automatisé depuis Python

Pour le principe de fonctionnement et les diagrammes d'architecture, voir
[AUTOMATION-ARCHITECTURE.md](AUTOMATION-ARCHITECTURE.md).

Ce wrapper est un **hôte GSD autonome**, indépendant de Kilo, Cline et Claude Code.
Il lit les commandes dans `commands/gsd`, charge leurs `execution_context`, suit
les workflows de `gsd-core/workflows`, charge les agents dans `agents`, et exécute
les utilitaires réels via `node gsd-core/bin/gsd-tools.cjs`. Il ne réécrit pas les
workflows en Python et ne dépend pas de l'ancien export SDK.

## Démarrage

Prérequis : Python 3.11+, Node 24+, Git, Bash (Git Bash sous Windows), et un endpoint
Chat Completions avec **appels d'outils**. Une compatibilité avec la génération de
texte seule ne suffit pas. Le transport utilise le protocole
[Chat Completions / function calling](https://developers.openai.com/api/docs/guides/function-calling).
Il n'impose ni SDK OpenAI, ni modèle OpenAI, ni Responses API.

Depuis ce dépôt :

```sh
npm ci
npm run build
python -m pip install -e .
mkdir -p tmp/gsd-demo
```

Copier et adapter `examples/automation/config.toml` et `rules.toml` : endpoint,
nom de modèle, dossier du projet et commandes d'acceptation. Les chemins de la
configuration, y compris les options de chemin de la CLI lorsqu'une configuration
est fournie, sont résolus relativement au fichier de configuration.

```sh
export OPENAI_API_KEY="..."  # Facultatif pour un serveur local sans authentification
gsd-auto --config examples/automation/config.toml --check
gsd-auto --config examples/automation/config.toml
gsd-auto --config examples/automation/config.toml --prompt "Mon besoin initial"
```

Sans installation Python, remplacer `gsd-auto` par `python -m gsd_automated`.
Sous PowerShell, créer le répertoire avec `New-Item -ItemType Directory -Force
tmp/gsd-demo`, définir la clé avec `$env:OPENAI_API_KEY = "..."`, et configurer
`shell = "C:/Program Files/Git/bin/bash.exe"`. Python, Node et Git doivent être
accessibles dans le PATH du processus. Un chemin absolu est accepté pour `node`.

On peut remplacer `prompt` par `prompt_file = "besoin.md"` (UTF-8). Sans besoin
dans la configuration ni `--prompt`, une unique saisie initiale est demandée si
stdin est un terminal. Aucun `input()` n'est utilisé ensuite. En CI, un besoin
absent produit une erreur immédiate. JSON est également accepté pour la config
et les règles. Sans config : fournir `--rules`, `--workspace`, `--gsd-root`, puis
`OPENAI_BASE_URL`, `OPENAI_MODEL` et `--prompt`.

### Endpoint OpenRouter

Le transport Chat Completions fonctionne avec OpenRouter :

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-sonnet-4"   # Identifiant fournisseur/modèle, ou "openrouter/auto"
```

Choisir un modèle dont la fiche OpenRouter indique la prise en charge des outils
(tool calling). La clé est lue depuis `OPENROUTER_API_KEY` : sur l'hôte
openrouter.ai cette variable est sélectionnée automatiquement quand
`api_key_env` n'est pas configuré et que `OPENAI_API_KEY` n'est pas définie.
`OPENROUTER_BASE_URL` et `OPENROUTER_MODEL` servent de variables de secours après
`OPENAI_BASE_URL` et `OPENAI_MODEL` ; si seule `OPENROUTER_API_KEY` est présente,
`base_url` vaut l'endpoint OpenRouter par défaut. Les en-têtes d'attribution
`HTTP-Referer` et `X-Title` sont envoyés avec des valeurs par défaut, remplaçables
via `[llm.headers]` qui accepte tout en-tête additionnel. `llm.request_options`
accepte les champs OpenRouter (`provider` pour le routage, `transforms`,
`reasoning`, etc.) sauf les clés réservées aux messages et aux outils. Les champs
`reasoning`/`reasoning_details` des réponses sont conservés et renvoyés à la
requête suivante, exigence des modèles à raisonnement avec appels d'outils.
Une erreur fournisseur relayée dans un corps HTTP 200 suit la même
classification qu'une erreur de transport (réessai, compaction ou arrêt).
Une réponse tronquée (`finish_reason: "length"`) est réémise avec un
`max_tokens` doublé (jusqu'à 131072, trois essais) au lieu d'arrêter le run.

## Suivi console

Chaque événement est aussi affiché en direct sur stderr : sessions et
sous-agents, appels d'outils avec un aperçu des arguments, résumés de résultats,
décisions du représentant, tokens consommés par appel (`USAGE`, total cumulé),
reconnexions, compactions et revue finale. `runtime.console = false` ou
`--quiet` désactive cet affichage ; `events.jsonl` reste complet dans tous les
cas.

## Exécution et décisions

1. Le contrôle local vérifie le corpus, Git, Node, Bash et l'identité du runtime GSD.
2. Le LLM hôte reçoit `new-project`, ou `progress` si une roadmap existe déjà.
3. Il exécute les instructions et poursuit les phases jusqu'à la livraison.
4. `AskUserQuestion` est intercepté. Les règles `answers` sont essayées dans
   l'ordre, par sous-chaîne insensible à la casse dans la question sérialisée.
   Sinon, un LLM représentant l'utilisateur reçoit besoin, règles, décisions
   antérieures et contexte récent. Il répond à la place de l'humain.
5. Une réponse libre du LLM hôte est aussi transmise au représentant : elle ne
   termine jamais implicitement la tâche. Les sous-agents doivent utiliser
   `AskUserQuestion` pour leurs questions et retourner leur résultat en texte.
6. `Finish` déclenche les commandes d'acceptation configurées et une revue LLM
   distincte du besoin et des preuves lues sur disque. Les échecs retournent au
   LLM hôte pour correction. La revue est une appréciation LLM, pas une preuve
   formelle : fournir des commandes d'acceptation pour les critères mesurables.

Par défaut, hôte, représentant et sous-agents utilisent le même modèle et endpoint.
`llm.decision_model` et `llm.agent_models` permettent de différencier les modèles
sur cet endpoint. `llm.request_options` passe des options fournisseur additionnelles
sans pouvoir remplacer les messages ou les outils. Les requêtes 429/5xx réessayées
comptent dans le budget global, partagé entre tous les rôles.

## Coupures réseau et compaction automatique

Une perte de connexion au fournisseur (VPN, timeout, connexion réinitialisée,
réponse HTTP coupée) déclenche automatiquement une nouvelle tentative de la
**même requête LLM**, avec les mêmes messages et résultats d'outils. Aucun prompt
« resume » ou « continue » humain n'est nécessaire tant que le programme tourne
et que la connexion revient dans les limites configurées. HTTP 408, 429 et les
erreurs temporaires 500/502/503/504 sont aussi réessayés. Une erreur de clé ou de
configuration (par exemple 401/403) arrête explicitement le programme.

Les tentatives attendent 1, 2, 4… secondes, puis au maximum 30 secondes entre
elles, sauf `Retry-After` plus long demandé par le serveur. Limites par requête :
30 tentatives et 900 secondes, configurables avec `reconnect_attempts`,
`reconnect_timeout`, `retry_initial_delay`, `retry_max_delay`. Le budget global
`max_calls` reste applicable. La console et la trace signalent l'interruption
et le retour de connexion. Le transport est non-streaming : une réponse partielle
n'est jamais utilisée pour exécuter un outil. Une requête LLM réessayée peut
cependant être facturée plusieurs fois par le fournisseur.

La reprise réseau reste à l'intérieur de l'appel LLM : elle ne relance ni les
scripts déjà terminés, ni les sous-agents déjà revenus, ni les contrôles
d'acceptation déjà exécutés avant cet appel. Les sous-agents, le représentant,
le résumeur et le réviseur bénéficient du même mécanisme.

Avant un appel LLM, le wrapper vérifie la taille du contexte et des définitions
d'outils. À 80 % de `max_context_chars`, il résume les tours anciens avec le modèle
du rôle courant et vise 50 % du budget. Les paramètres sont
`compact_trigger_ratio` et `compact_target_ratio`. Il conserve les instructions
système, un résumé de travail (tâche, décisions, actions effectuées, fichiers,
tests, questions et prochaines étapes) et les échanges récents complets.
Un appel d'outil et ses résultats ne sont jamais séparés. Le programme continue
ensuite automatiquement, sans réinitialiser ses budgets ni sa pile de sous-agents.

La mesure est en **caractères**, avec une marge configurable, pas en tokens :
elle reste indépendante du tokenizer du fournisseur. Si celui-ci refuse malgré
tout le contexte (`context_length_exceeded` ou message reconnu équivalent), le
wrapper compacte davantage et réessaie jusqu'à trois fois. Les longs historiques
sont résumés par fragments. Les appels de résumé utilisent les mêmes budgets
et la même reconnexion. Une compaction impossible (instructions immuables trop
longues, résumé invalide ou trop long) provoque une erreur explicite ; elle ne
détruit pas l'historique initial. La qualité du résumé dépend du modèle.

Avant chaque requête, un checkpoint `session-<id>.json` est écrit atomiquement
dans le répertoire de l'exécution. Avant chaque compaction, le contexte complet
est conservé dans `context-<id>.json`. L'agent reçoit le chemin de cette archive
pour retrouver les détails nécessaires. Ces fichiers peuvent contenir du code
et des données du projet, comme la trace.

## Outils et portée de cette version

`Read` (pagination en caractères), `Write`, `Edit` (occurrence unique), `Glob`,
`Grep` (texte littéral), `Bash`, `GSD` (argv sans shell), `SlashCommand`, `Agent`,
`AskUserQuestion`, `Finish`. Les sous-agents ont leur propre conversation et les
mêmes outils, sauf `Finish`. Leur exécution est **synchrone et séquentielle**,
y compris les vagues ; pas de parallélisme ni de worktrees automatiques.
Les workflows restent interprétés par le LLM, comme dans les hôtes habituels.
Les hooks Claude/Kilo/Cline, MCP, navigateur interactif et fonctionnalités
spécifiques à ces hôtes ne sont pas réimplémentés. Les fallbacks documentés GSD
restent disponibles via scripts ; une capacité indispensable indisponible doit
produire un blocage. La recherche `GSD websearch` nécessite sa propre configuration.

Les scripts reçoivent stdin fermé, un délai maximal et un arrêt de l'arbre des
processus en cas de dépassement. Sous Windows, si le système refuse `taskkill`,
le processus direct est tué ; l'arrêt des descendants ne peut alors être garanti.
Le shell est neuf à chaque appel ; `gsd_run`
est fourni automatiquement dans Bash. PowerShell peut être configuré, mais le LLM
devra traduire les extraits Bash ; Git Bash est préférable pour ce corpus.

Les outils de fichiers limitent leurs chemins au projet et au corpus. **Le shell
est une exécution de code locale avec les droits du processus, pas un bac à sable.**
Les règles guident les décisions LLM, elles ne constituent pas une politique OS.
Utiliser un conteneur ou une VM pour une isolation forte. La variable contenant
la clé LLM est retirée de l'environnement des scripts, mais ce n'est pas une
isolation contre un code qui lirait d'autres fichiers ou secrets de la machine.

## Suivi, reprise et statuts

Les événements et décisions sont dans `.gsd-auto/<run-id>/events.jsonl`, le résultat
final dans `result.json`, et l'état GSD reste dans `.planning/`. La trace contient
des contenus de projet : la conserver localement. La clé LLM configurée est
masquée dans les événements. Il n'y a pas d'exécution simultanée autorisée sur le
même projet : un verrou exclusif l'empêche. Après un arrêt brutal, supprimer
`.gsd-auto/run.lock` seulement après avoir vérifié que le PID enregistré est arrêté.

```sh
gsd-auto --config examples/automation/config.toml --resume
python -m unittest discover -s tests_python -v
```

La reprise après arrêt du programme crée une nouvelle conversation à partir des
fichiers GSD, des décisions enregistrées et des chemins des checkpoints du dernier
run correspondant au même besoin. Elle n'est pas une reprise exacte de pile ni une garantie
« exactement une fois » pour les commandes interrompues. Le LLM doit examiner
les modifications partielles avant de réessayer. Conserver le même besoin et les
mêmes règles pour une reprise cohérente.

Codes de sortie : `0` livraison acceptée (ou contrôle local réussi), `1` erreur
technique/budget, `2` blocage déclaré, `130` interruption clavier. `--check` ne
contacte pas le LLM et ne valide donc ni la clé ni la capacité d'appels d'outils.
Les limites arrêtent explicitement la tâche ; elles ne transforment pas une
exécution incomplète en succès.

## Validation de cette version

Les tests couvrent notamment le protocole HTTP Chat Completions, les questions
structurées et libres, les règles, les contextes de sous-agents, les erreurs
d'outils, les délais de processus, les budgets, le verrou, la reprise et la
validation finale, les reconnexions après coupure, les limites de reconnexion,
la compaction répétée et les refus de contexte du fournisseur. Un test lance la vraie CLI Python avec un endpoint simulé,
le corpus GSD du dépôt, `query init.new-project`, Git Bash, Node, un sous-agent
qui écrit un fichier et une commande d'acceptation réelle. Ce test nécessite
la compilation préalable de GSD ; sinon il est explicitement ignoré.

Un autre serveur de test coupe réellement la connexion après une écriture :
la requête est réémise avec son résultat d'outil et l'écriture ne se produit
qu'une fois. Les tests de compaction vérifient aussi les groupes d'appels
d'outils multiples, les archives et la conservation de l'original en cas d'échec.

La compilation complète de GSD et la suite de tests ont réussi sous Windows.
Aucun parcours avec un vrai LLM n'a été effectué : la qualité du suivi autonome
de toutes les phases et la compatibilité d'un fournisseur particulier restent
à valider sur le modèle choisi.
