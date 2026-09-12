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
budget de sortie doublé (jusqu'à `max_output_tokens`, 32768 par défaut,
trois escalades). `max_completion_tokens` est également respecté. Au plafond,
la réponse partielle est écartée et le modèle reçoit une consigne de découpage
en appels plus petits ; aucun outil partiel n'est exécuté. Les résumés ne sont
jamais réémis pour une sortie tronquée.
Une complétion vide (ni contenu ni outil — hoquet du fournisseur) est réémise
jusqu'à trois fois ; ensuite le tour vide est écarté de l'historique : à la
racine le modèle est relancé par un message de continuation, et un sous-agent
retourne `{"error": ...}` au parent qui peut réessayer ou faire la tâche
lui-même, sans interrompre le run.

## Suivi console

Chaque événement est aussi affiché en direct sur stderr : sessions et
sous-agents, appels d'outils avec un aperçu des arguments, résumés de résultats,
décisions du représentant, tokens consommés par appel (`USAGE`, total cumulé),
reconnexions, compactions et revue finale. Le transport étant non-streaming,
une ligne `WAIT <modèle> <secondes>s` apparaît toutes les 30 s pendant un appel
en vol : un silence prolongé est donc une génération longue, pas un blocage.
`runtime.console = false` ou `--quiet` désactive cet affichage ; `events.jsonl`
reste complet dans tous les cas.

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
6. `Finish` refuse les preuves constituées uniquement de planning/traces ou de
   fichiers vides, puis déclenche les commandes d'acceptation configurées et une revue LLM
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

Avant un appel LLM, l'hôte calcule le budget utilisable à partir de
`context_window_tokens` (256000 par défaut), de la réserve `max_output_tokens`
(32768) et des définitions d'outils. Il conserve une marge de 10 % et commence
avec une estimation prudente de 0,5 token par caractère sérialisé. Les usages
réels du fournisseur peuvent augmenter ce ratio, avec une marge supplémentaire
de 20 %. `max_context_chars` reste un plafond indépendant pour limiter le coût.
L'estimation ne remplace pas le tokenizer du fournisseur : un refus de contexte
entraîne une réduction supplémentaire, mémorisée pour les prochains appels du
modèle et sauvegardée pour la reprise. Utiliser la plus petite fenêtre des
modèles routés dans une configuration commune.

À 80 % du budget effectif (`compact_trigger_ratio`), la compaction vise au plus
50 % (`compact_target_ratio`). Elle archive l'intégralité de l'historique, garde
les messages système et les tours récents complets, puis produit **un seul résumé**
d'un condensé de 32000 caractères maximum. Les raisonnements anciens restent
dans l'archive ; les échanges récents gardent leurs champs de raisonnement et
leurs paires appels/résultats intactes. La sortie du résumé est bornée par
`summary_max_tokens = 2048` et `summary_max_chars = 6000`. Il n'y a ni boucle de
résumés par fragments, ni appel de réparation d'un résumé trop long.

Un résumé vide, tronqué, trop long ou indisponible utilise des extraits archivés
comme solution de repli. La requête de résumé utilise une fenêtre de reconnexion
limitée à 60 secondes, sous réserve du délai de lecture réseau en cours. Les
instructions immuables impossibles à faire tenir restent une erreur explicite.
La compaction reste une opération avec perte : le modèle doit consulter les
fichiers GSD ou l'archive pour retrouver un détail omis. L'affectation initiale
complète est aussi conservée dans `assignment-*.json`, référencée par le système.

La console distingue `COMPACT START`, `COMPACT`, `COMPACT FALLBACK` et
`USAGE[summary]`. `CONTEXT` affiche le budget courant ; `cumulative` est la somme
des tokens facturés sur les appels du lancement, **pas la taille du contexte**.

Un checkpoint atomique `checkpoint.json` sauvegarde la pile des sessions, les
réponses modèle, les outils en attente et les résultats terminés. Il est écrit
avant chaque effet d'outil et après chaque résultat. `session-*.json` reste un
instantané des requêtes ; `context-*.json` conserve l'historique avant compaction.
Ces fichiers peuvent contenir du code et des données du projet, comme la trace.

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

`--resume` restaure la pile des sessions du dernier run correspondant exactement
au besoin initial, jusqu'au sous-agent interrompu. Les résultats d'outils déjà
checkpointés sont conservés ; un sous-agent terminé n'est pas réexécuté. Le
nouveau lancement a ses propres budgets de consommation (`max_calls` et
`max_steps`) et son propre répertoire de trace ; il conserve les règles et les
décisions. Des règles différentes provoquent une erreur explicite.

Pour les anciennes traces sans `checkpoint.json`, la reprise reconstruit les
sessions depuis `session-*.json` et les événements postérieurs aux instantanés.
Le mode est nommé `legacy_reconstructed` dans le log. Les anciennes traces sans
historique exploitable utilisent encore les fichiers GSD et les décisions ;
le log affiche alors zéro session restaurée. Sans run correspondant au besoin,
`--resume` échoue explicitement. Un démarrage raté ne masque pas le dernier run
exploitable.

Si un outil avait commencé mais que son résultat n'est pas enregistré, son effet
est **inconnu**. Le modèle reçoit ce statut et doit inspecter les fichiers avant
une nouvelle action. L'hôte ne rejoue pas automatiquement une commande shell
incertaine ni la suite de son lot. Il n'existe pas de garantie « exactement une
fois » pour un effet externe interrompu entre son exécution et son checkpoint.

Vérifier la reprise sans appeler le modèle ni modifier le projet :

```sh
gsd-auto --config config.toml --resume --check --prompt "Le besoin initial exact"
```

La sortie indique `source`, `mode`, les agents, le nombre de messages et les
outils en attente. `RESUME` confirme ces informations au lancement ; `resume.json`
conserve la provenance. Après un arrêt brutal, le verrou reste volontairement
à vérifier avant suppression ; une erreur réseau gérée libère le verrou.

Pour le projet local `dofus-stuff-machine`, placé à côté de ce dépôt, le profil
`examples/automation/dofus-docs.toml` conserve le modèle, les règles existantes
et le besoin exact. Il réserve une fenêtre de 256k, autorise jusqu'à 2000 appels
et 500 étapes par session/lancement, et passe le timeout réseau à 600 secondes
avec une fenêtre de reconnexion de 1800 secondes. Ces plafonds limitent la
consommation ; ils ne garantissent pas qu'un besoin arbitraire tient dedans.
Depuis `gsd-core-automated` :

```powershell
uv run python -m gsd_automated --config examples/automation/dofus-docs.toml --resume --check
uv run python -m gsd_automated --config examples/automation/dofus-docs.toml --resume
```

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

Les tests de reprise lancent deux processus CLI successifs contre un endpoint
simulé : écriture d'un guide français, arrêt après cette écriture, inspection
`--resume --check`, reprise du sous-agent sans rejouer l'écriture, commande
d'acceptation Node réelle et livraison acceptée. D'autres tests couvrent les
lots d'outils partiellement terminés, les effets incertains, le résultat d'un
sous-agent non encore transmis au parent et la migration des anciennes traces.
Le log et les sorties des sous-processus Python utilisent UTF-8 sous Windows.

La validation locale n'exécute pas le modèle OpenRouter réel : elle prouve les
mécanismes de l'hôte, pas la qualité de la documentation finale ni la disponibilité
continue du fournisseur. Le run documentaire reste à relancer par l'utilisateur.
