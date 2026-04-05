    const state = {
      conversations: [],
      opportunities: [],
      selectedItem: null,
      selectedDetail: null,
      profile: null,
      sync: null,
      syncRefreshPending: false,
      draft: {
        itemKey: null,
        text: "",
        loading: false,
        visible: false,
        note: "",
      },
      focusDraftOnRender: false,
      listView: "active",
    };

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatInlineMarkdown(value) {
      const escaped = escapeHtml(value);
      return escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    }

    function renderRichText(value) {
      const source = String(value || "").replaceAll("\\r\\n", "\\n").trim();
      if (!source) {
        return "";
      }

      const lines = source.split("\\n");
      const blocks = [];
      let paragraph = [];
      let listItems = [];

      const flushParagraph = () => {
        if (!paragraph.length) return;
        blocks.push(`<p>${paragraph.map((line) => formatInlineMarkdown(line)).join("<br>")}</p>`);
        paragraph = [];
      };

      const flushList = () => {
        if (!listItems.length) return;
        blocks.push(`<ul>${listItems.map((item) => `<li>${formatInlineMarkdown(item)}</li>`).join("")}</ul>`);
        listItems = [];
      };

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          flushParagraph();
          flushList();
          continue;
        }

        if (/^[-*]\s+/.test(line)) {
          flushParagraph();
          listItems.push(line.replace(/^[-*]\s+/, ""));
          continue;
        }

        flushList();
        paragraph.push(line);
      }

      flushParagraph();
      flushList();
      return blocks.join("");
    }

    function truncate(value, max = 120) {
      const text = String(value || "").trim();
      if (!text) return "";
      return text.length > max ? `${text.slice(0, max)}…` : text;
    }

    function formatDate(value) {
      if (!value) return "-";
      return new Date(value).toLocaleString("fr-FR");
    }

    function formatRelative(value) {
      if (!value) return "-";
      const date = new Date(value);
      const diffMinutes = Math.max(1, Math.round((Date.now() - date.getTime()) / 60000));
      if (diffMinutes < 60) return `${diffMinutes} min`;
      const diffHours = Math.round(diffMinutes / 60);
      if (diffHours < 24) return `${diffHours} h`;
      const diffDays = Math.round(diffHours / 24);
      return `${diffDays} j`;
    }

    function smartTierRank(tier) {
      if (!tier || !tier.id) return 2;
      if (tier.id === "hot") return 0;
      if (tier.id === "follow_up") return 1;
      return 2;
    }

    function feedItemTierRank(item) {
      if (item.kind === "conversation") {
        return smartTierRank(item.raw.smart_tier);
      }
      const s = item.raw.strength?.score;
      if (s == null) return 2;
      if (s >= 8) return 0;
      if (s >= 5) return 1;
      return 2;
    }

    function strengthScoreValue(raw) {
      const n = raw?.strength?.score;
      return typeof n === "number" && !Number.isNaN(n) ? n : 0;
    }

    /** Texte pour l’attribut title (hover) sur le badge score. */
    function strengthHoverTitle(strength) {
      if (!strength) return "";
      const parts = [];
      if (strength.explanation) {
        parts.push(strength.explanation);
      }
      if (Array.isArray(strength.why) && strength.why.length) {
        parts.push(strength.why.join(" · "));
      }
      return parts.join("\n\n");
    }

    function renderStrengthInsightBlock(strength, strengthExtraClass = "") {
      if (!strength) return "";
      const hover = strengthHoverTitle(strength);
      const sc = String(strengthExtraClass || "").trim();
      const strengthClass = sc ? `crm-strength ${sc}` : "crm-strength";
      const whyList = Array.isArray(strength.why) && strength.why.length
        ? `<ul class="crm-why">${strength.why.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`
        : "";
      const actions = Array.isArray(strength.suggested_actions) && strength.suggested_actions.length
        ? `<div class="crm-suggestions"><span class="crm-suggestions-label">Pistes d’action</span><ul>${strength.suggested_actions.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul></div>`
        : "";
      return `
        <p class="${escapeHtml(strengthClass)}" ${hover ? `title="${escapeHtml(hover)}"` : ""}>${escapeHtml(strength.label)}</p>
        ${strength.explanation ? `<p class="crm-score-explanation">${escapeHtml(strength.explanation)}</p>` : ""}
        ${whyList}
        ${actions}
      `;
    }

    function fetchJson(url, options) {
      return fetch(url, options).then(async (response) => {
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error || "Request failed");
        }
        return response.json();
      });
    }

    function statusForConversation(conversation) {
      if (conversation.archived_at || conversation.status === "closed") {
        return { label: "Archivé", css: "archived" };
      }
      if (conversation.workflow_status === "a_repondre") {
        return { label: "Action requise", css: "reply" };
      }
      return { label: "En attente", css: "waiting" };
    }

    function summaryForConversation(conversation) {
      if (conversation.follow_up_due) {
        return truncate("Aucun retour depuis plusieurs jours. Une relance courte est prête.");
      }
      if (conversation.workflow_status === "attente_reponse") {
        return truncate(
          conversation.next_action
          || conversation.ai_next_action
          || "En attente du retour du client."
        );
      }
      if (conversation.workflow_status === "a_repondre") {
        return truncate(
          conversation.next_action
          || conversation.ai_next_action
          || conversation.ai_summary
          || conversation.last_message
          || "Réponse attendue."
        );
      }
      return truncate(
        conversation.ai_summary
        || conversation.next_action
        || conversation.last_message
        || "Aucun résumé disponible."
      );
    }

    function statusForOpportunity(opportunity) {
      if (opportunity.archived_at) {
        return { label: "Archivé", css: "archived" };
      }
      if (opportunity.ai_should_reply === false) {
        return { label: "En attente", css: "waiting" };
      }
      return { label: "Action requise", css: "offer" };
    }

    function summaryForOpportunity(opportunity) {
      return truncate(
        opportunity.ai_summary
        || opportunity.description
        || "Nouvelle opportunité reçue."
      );
    }

    function itemKey(kind, id) {
      return `${kind}:${id}`;
    }

    const MS_PER_DAY = 86400000;

    function lastActivityMsFromRaw(raw) {
      const s = raw?.last_message_at || raw?.updated_at;
      const t = new Date(s).getTime();
      return Number.isFinite(t) ? t : 0;
    }

    /** Âge en jours (flottant) depuis last_message_at ou updated_at. */
    function ageDaysFromConversationRaw(raw) {
      const t = lastActivityMsFromRaw(raw);
      if (!t) return Infinity;
      return (Date.now() - t) / MS_PER_DAY;
    }

    function formatConversationAgeLabel(raw) {
      const ref = new Date(raw?.last_message_at || raw?.updated_at);
      if (!Number.isFinite(ref.getTime())) return "";
      const now = new Date();
      const startRef = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate());
      const startNow = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const diffDays = Math.round((startNow - startRef) / MS_PER_DAY);
      if (diffDays <= 0) return "aujourd'hui";
      return `il y a ${diffDays} j`;
    }

    /**
     * Regroupe les lignes « conversation » (feed items) par score et ancienneté.
     * @param {Array<{ kind: string, raw: object }>} conversationItems
     */
    function getGroupedConversations(conversationItems) {
      const enriched = conversationItems.map((item) => {
        const ageDays = ageDaysFromConversationRaw(item.raw);
        const score = strengthScoreValue(item.raw);
        const lastT = lastActivityMsFromRaw(item.raw);
        return { item, ageDays, score, lastT };
      });
      const prioritaire = enriched
        .filter((x) => x.score >= 7 && x.ageDays <= 7)
        .sort((a, b) => b.score - a.score)
        .map((x) => x.item);
      const recent = enriched
        .filter((x) => !(x.score >= 7 && x.ageDays <= 7) && x.ageDays <= 7)
        .sort((a, b) => b.lastT - a.lastT)
        .map((x) => x.item);
      const ancien = enriched
        .filter((x) => x.ageDays > 7)
        .sort((a, b) => b.lastT - a.lastT)
        .map((x) => x.item);
      return { prioritaire, recent, ancien };
    }

    function buildConversationFeedItems() {
      return state.conversations
        .filter((conversation) => state.listView === "archived"
          ? Boolean(conversation.archived_at || conversation.status === "closed")
          : !Boolean(conversation.archived_at || conversation.status === "closed"))
        .map((conversation) => ({
          kind: "conversation",
          id: conversation.id,
          updated_at: conversation.updated_at,
          title: conversation.client_name,
          summary: summaryForConversation(conversation),
          status: statusForConversation(conversation),
          raw: conversation,
        }));
    }

    function buildOpportunityFeedItems() {
      return state.opportunities
        .filter((opportunity) => state.listView === "archived"
          ? Boolean(opportunity.archived_at)
          : !Boolean(opportunity.archived_at))
        .map((opportunity) => ({
          kind: "opportunity",
          id: opportunity.id,
          updated_at: opportunity.updated_at,
          title: opportunity.title,
          summary: summaryForOpportunity(opportunity),
          status: statusForOpportunity(opportunity),
          raw: opportunity,
        }));
    }

    function sortOpportunityFeedItems(items) {
      return [...items].sort((left, right) => {
        const sb = strengthScoreValue(right.raw) - strengthScoreValue(left.raw);
        if (sb !== 0) return sb;
        const tr = feedItemTierRank(left) - feedItemTierRank(right);
        if (tr !== 0) return tr;
        const leftRank = left.status.label === "Action requise" ? 0 : 1;
        const rightRank = right.status.label === "Action requise" ? 0 : 1;
        if (leftRank !== rightRank) return leftRank - rightRank;
        return new Date(right.updated_at) - new Date(left.updated_at);
      });
    }

    function sortConversationFeedItemsFlat(items) {
      return [...items].sort((left, right) => {
        const sb = strengthScoreValue(right.raw) - strengthScoreValue(left.raw);
        if (sb !== 0) return sb;
        const tr = feedItemTierRank(left) - feedItemTierRank(right);
        if (tr !== 0) return tr;
        const leftRank = left.status.label === "Action requise" ? 0 : 1;
        const rightRank = right.status.label === "Action requise" ? 0 : 1;
        if (leftRank !== rightRank) return leftRank - rightRank;
        return new Date(right.updated_at) - new Date(left.updated_at);
      });
    }

    /** Ordre plat = ordre d’affichage (navigation clavier, sélection par défaut). */
    function feedItems() {
      const opps = sortOpportunityFeedItems(buildOpportunityFeedItems());
      const conv = buildConversationFeedItems();
      if (state.listView === "archived") {
        return [...opps, ...sortConversationFeedItemsFlat(conv)];
      }
      const g = getGroupedConversations(conv);
      return [...opps, ...g.prioritaire, ...g.recent, ...g.ancien];
    }

    function renderFeedRow(item, rowExtraClass) {
      const active = state.selectedItem
        && state.selectedItem.kind === item.kind
        && state.selectedItem.id === item.id;
      const extra = rowExtraClass ? ` ${rowExtraClass}` : "";
      const ageDays = item.kind === "conversation" ? ageDaysFromConversationRaw(item.raw) : 0;
      const ageLabel = item.kind === "conversation" ? formatConversationAgeLabel(item.raw) : "";
      const staleBadge = item.kind === "conversation" && ageDays > 14
        ? `<span class="badge stale-thread" title="Dernière activité il y a plus de 14 jours">⚠️ ancien</span>`
        : "";
      const ageSpan = item.kind === "conversation" && ageLabel
        ? `<span class="row-age">${escapeHtml(ageLabel)}</span>`
        : "";
      return `
          <article class="row${extra} ${active ? "active" : ""}" data-kind="${escapeHtml(item.kind)}" data-id="${escapeHtml(item.id)}">
            <div class="row-top">
              <div class="row-main">
                <p class="client-name">${escapeHtml(item.title)}</p>
              </div>
              <div class="row-time-block">
                <div class="time">${escapeHtml(formatRelative(item.updated_at))}</div>
                ${ageSpan}
              </div>
            </div>
            <p class="summary">${escapeHtml(item.summary)}</p>
            <div class="row-actions">
              ${item.kind === "conversation" && item.raw.smart_tier
          ? `<span class="badge tier tier-${escapeHtml(item.raw.smart_tier.id)}">${escapeHtml(item.raw.smart_tier.emoji)} ${escapeHtml(item.raw.smart_tier.label)}</span>`
          : ""}
              ${item.raw.strength
          ? `<span class="badge strength-score" title="${escapeHtml(strengthHoverTitle(item.raw.strength))}">${escapeHtml(item.raw.strength.label)}</span>`
          : ""}
              ${staleBadge}
              <span class="badge ${escapeHtml(item.status.css)}">${escapeHtml(item.status.label)}</span>
              ${item.kind === "opportunity" && item.raw.ai_fit_score != null ? `<span class="badge fit">Fit ${escapeHtml(Math.round(item.raw.ai_fit_score))}</span>` : ""}
            </div>
          </article>
        `;
    }

    function workflowValueForConversation(conversation) {
      if (conversation.archived_at || conversation.status === "closed") {
        return "archived";
      }
      if (conversation.workflow_status === "a_repondre") {
        return "a_repondre";
      }
      return "attente_reponse";
    }

    function messageRole(message, conversation) {
      const sender = String(message.sender || "").toLowerCase();
      const client = String(conversation.client_name || "").toLowerCase();
      const freelancer = String(state.profile?.full_name || "").toLowerCase();
      if (sender && client && sender.includes(client)) return "incoming";
      if (sender && freelancer && sender.includes(freelancer)) return "outgoing";
      return "incoming";
    }

    function renderStatus(payload) {
      const previousSync = state.sync;
      state.sync = payload.sync;
      state.profile = payload.profile || null;
      const profile = payload.profile;
      const dot = document.getElementById("syncDot");
      const label = document.getElementById("syncLabel");
      const chip = document.getElementById("profileChip");
      dot.className = "sync-dot";

      if (payload.sync.running) {
        label.textContent = "Synchronisation…";
      } else if (payload.sync.last_error) {
        dot.classList.add("bad");
        label.textContent = "Erreur sync";
      } else {
        dot.classList.add("good");
        label.textContent = "À jour";
      }

      if (payload.sync.running) {
        state.syncRefreshPending = true;
      } else if (previousSync?.running && state.syncRefreshPending) {
        state.syncRefreshPending = false;
        window.setTimeout(() => {
          fullRefresh().catch(console.error);
        }, 150);
      }

      document.getElementById("syncButton").disabled = Boolean(payload.sync.running);
      if (profile) {
        chip.hidden = false;
        chip.href = profile.profile_url || "#";
        document.getElementById("profileName").textContent = profile.full_name || "Profil connecté";
        document.getElementById("profileRole").textContent = profile.headline || "Profil Malt";
        const avatar = document.getElementById("profileAvatar");
        avatar.src = profile.image_url || "";
        avatar.style.display = profile.image_url ? "block" : "none";
      } else {
        chip.hidden = true;
      }
    }

    function renderList() {
      const list = document.getElementById("inboxList");
      const items = feedItems();
      document.getElementById("listSummary").textContent = state.listView === "archived"
        ? `${items.length} archivés`
        : `${items.length} éléments — opportunités, puis conversations (prioritaires · récent · ancien)`;

      if (!items.length) {
        list.innerHTML = '<div class="empty">Aucun message ou opportunité synchronisé.</div>';
        return;
      }

      if (state.listView === "archived") {
        list.innerHTML = items.map((item) => renderFeedRow(item, "")).join("");
      } else {
        const opps = sortOpportunityFeedItems(buildOpportunityFeedItems());
        const conv = buildConversationFeedItems();
        const g = getGroupedConversations(conv);
        const blocks = [];
        if (opps.length) {
          blocks.push('<section class="inbox-section"><h3 class="inbox-section-title">Opportunités</h3>');
          blocks.push(opps.map((item) => renderFeedRow(item, "")).join(""));
          blocks.push("</section>");
        }
        const sections = [
          { key: "prioritaire", title: "🔥 Opportunités prioritaires", rows: g.prioritaire, wrapClass: "" },
          { key: "recent", title: "🕒 Récent", rows: g.recent, wrapClass: "" },
          { key: "ancien", title: "📦 Ancien", rows: g.ancien, wrapClass: "inbox-section-ancien" },
        ];
        for (const sec of sections) {
          if (!sec.rows.length) continue;
          blocks.push(
            `<section class="inbox-section ${sec.wrapClass}"><h3 class="inbox-section-title">${escapeHtml(sec.title)}</h3>`,
          );
          blocks.push(sec.rows.map((item) => renderFeedRow(item, "")).join(""));
          blocks.push("</section>");
        }
        list.innerHTML = blocks.join("");
      }

      list.querySelectorAll(".row").forEach((node) => {
        node.addEventListener("click", () => {
          selectItem(node.dataset.kind, node.dataset.id).catch(console.error);
        });
      });

    }

    function renderDetail() {
      const body = document.getElementById("detailBody");
      if (!state.selectedDetail?.kind) {
        document.getElementById("detailTitle").textContent = "Aucun élément sélectionné";
        document.getElementById("detailTime").textContent = "Choisis une ligne pour ouvrir le détail.";
        body.innerHTML = '<div class="empty">Clique sur une ligne pour ouvrir le détail.</div>';
        return;
      }

      if (state.selectedDetail.kind === "opportunity") {
        renderOpportunityDetail(body);
        return;
      }

      const conversation = state.selectedDetail.conversation;
      const messages = state.selectedDetail.messages || [];
      const status = statusForConversation(conversation);
      const workflowValue = workflowValueForConversation(conversation);
      const draftState = state.draft.itemKey === itemKey("conversation", conversation.id)
        ? state.draft
        : {
            itemKey: itemKey("conversation", conversation.id),
            text: conversation.ai_reply_draft || "",
            loading: false,
            visible: Boolean(conversation.ai_reply_draft),
            note: "",
          };
      const replyDraft = draftState.text || "";
      const showDraft = draftState.visible || draftState.loading || Boolean(replyDraft);
      const timeline = state.selectedDetail.timeline || [];

      document.getElementById("detailTitle").textContent = "Conversation";
      document.getElementById("detailTime").textContent = "Réponse et messages";

      body.innerHTML = `
        <div class="detail-toolbar">
          <div class="detail-meta">
            <div class="detail-heading">
              <h3>${escapeHtml(conversation.client_name)}</h3>
              <p>${escapeHtml(formatDate(conversation.updated_at))}</p>
              <span class="badge ${escapeHtml(status.css)}">${escapeHtml(status.label)}</span>
              ${conversation.smart_tier
          ? `<span class="badge tier tier-${escapeHtml(conversation.smart_tier.id)}">${escapeHtml(conversation.smart_tier.emoji)} ${escapeHtml(conversation.smart_tier.label)}</span>`
          : ""}
            </div>
            <div class="toolbar-actions">
              <button id="replyAnchorButton" type="button" class="primary-button" ${draftState.loading ? "disabled" : ""}>${draftState.loading ? "Génération..." : "Réponse rapide IA"}</button>
              <a class="detail-link" href="https://www.malt.fr/messages/${encodeURIComponent(conversation.id)}" target="_blank" rel="noopener noreferrer">Ouvrir Malt</a>
              <button id="archiveInlineButton" type="button" class="danger-button">${conversation.archived_at ? "Désarchiver" : "Archiver"}</button>
              <div class="more-actions">
                <button id="moreActionsButton" type="button" class="ghost-button">...</button>
                <div id="moreActionsMenu" class="more-menu">
                  <button id="aiRefreshButton" type="button" class="menu-button">Régénérer la réponse IA</button>
                </div>
              </div>
            </div>
          </div>
          <div class="crm-insight">
            ${renderStrengthInsightBlock(conversation.strength)}
            ${conversation.smart_tier && conversation.smart_tier.hint
        ? `<p class="crm-tier-hint">${escapeHtml(conversation.smart_tier.hint)}</p>`
        : ""}
            ${conversation.reminder_due_at
        ? `<p class="crm-reminder">Rappel : ${escapeHtml(formatDate(conversation.reminder_due_at))}</p>`
        : ""}
          </div>
          <div class="quick-actions">
            <button id="quickSentOk" type="button" class="ghost-button">Envoyé ✔</button>
            <button id="quickSnooze3d" type="button" class="ghost-button">Relancer dans 3 jours</button>
          </div>
          ${showDraft ? `
            <div id="draftCard" class="draft">
              <div class="draft-head">
                <h3>Réponse suggérée</h3>
                <span class="draft-status">${draftState.loading ? "Génération..." : (replyDraft.trim() ? "Prêt à envoyer" : "Aucune réponse suggérée")}</span>
              </div>
              ${draftState.note ? `<div class="draft-hint">${escapeHtml(draftState.note)}</div>` : ""}
              <textarea id="replyDraftField" ${draftState.loading ? "disabled" : ""} placeholder="La réponse IA apparaîtra ici.">${escapeHtml(replyDraft)}</textarea>
              <div class="draft-actions">
                <span class="draft-hint">Copier et ouvrir dans Malt.</span>
                <div class="draft-buttons">
                  <button id="sendReplyButton" type="button" class="primary-button" ${draftState.loading || !replyDraft.trim() ? "disabled" : ""}>Copier et ouvrir dans Malt</button>
                  <button id="copyReplyButton" type="button" class="ghost-button" ${draftState.loading || !replyDraft.trim() ? "disabled" : ""}>Copier</button>
                </div>
              </div>
            </div>
          ` : ""}
          <div class="detail-actions">
            <div class="field">
              <label for="workflowSelect">Statut</label>
              <select id="workflowSelect">
                <option value="a_repondre" ${workflowValue === "a_repondre" ? "selected" : ""}>Action requise</option>
                <option value="attente_reponse" ${workflowValue === "attente_reponse" ? "selected" : ""}>En attente</option>
                <option value="archived" ${workflowValue === "archived" ? "selected" : ""}>Archivé</option>
              </select>
            </div>
            <div class="inline-actions">
              <span class="save-hint">Le statut peut être corrigé manuellement.</span>
              <button id="saveWorkflowButton" type="button" class="save-button">Enregistrer</button>
            </div>
          </div>
        </div>
        <p class="detail-summary">${escapeHtml(summaryForConversation(conversation))}</p>
        <p class="thread-title">Messages</p>
        <div class="thread">
          ${messages.length ? messages.map((message) => {
            const role = messageRole(message, conversation);
            return `
              <article class="message ${role}">
                <div class="message-meta">
                  <span>${escapeHtml(message.sender)}</span>
                  <span>${escapeHtml(formatDate(message.created_at))}</span>
                </div>
                <div class="message-body">${renderRichText(message.content || "")}</div>
              </article>
            `;
          }).join("") : '<div class="empty">Aucun message synchronisé.</div>'}
        </div>
        <p class="thread-title">Activité CRM</p>
        <div class="timeline">
          ${timeline.length
        ? timeline.map((ev) => `
            <article class="timeline-event kind-${escapeHtml(ev.kind)}">
              <div class="timeline-meta">
                <span class="timeline-title">${escapeHtml(ev.title)}</span>
                <span class="timeline-when">${escapeHtml(formatDate(ev.created_at))}</span>
              </div>
              ${ev.detail ? `<p class="timeline-detail">${escapeHtml(ev.detail)}</p>` : ""}
            </article>
          `).join("")
        : '<div class="empty">Historique vide — utilise les boutons ci-dessus ou change le statut.</div>'}
        </div>
      `;

      body.scrollTop = 0;

      const moreButton = document.getElementById("moreActionsButton");
      const moreMenu = document.getElementById("moreActionsMenu");
      moreButton.addEventListener("click", (event) => {
        event.stopPropagation();
        moreMenu.classList.toggle("open");
      });
      document.addEventListener("click", () => {
        moreMenu.classList.remove("open");
      }, { once: true });

      document.getElementById("quickSentOk").addEventListener("click", async () => {
        try {
          const updated = await postConversationQuickAction(conversation.id, "message_sent");
          mergeConversation(updated);
          await selectItem("conversation", conversation.id);
        } catch (error) {
          alert(error.message);
        }
      });

      document.getElementById("quickSnooze3d").addEventListener("click", async () => {
        try {
          const updated = await postConversationQuickAction(conversation.id, "snooze_3d");
          mergeConversation(updated);
          await selectItem("conversation", conversation.id);
        } catch (error) {
          alert(error.message);
        }
      });

      document.getElementById("archiveInlineButton").addEventListener("click", async () => {
        try {
          const updated = await updateConversationCRM(conversation.id, {
            archived: !conversation.archived_at,
          });
          mergeConversation(updated);
          state.selectedDetail.conversation = updated;
          await selectItem("conversation", conversation.id);
        } catch (error) {
          alert(error.message);
        }
      });

      document.getElementById("saveWorkflowButton").addEventListener("click", async () => {
        const workflow = document.getElementById("workflowSelect").value;
        const payload = workflow === "archived"
          ? { archived: true }
          : {
              archived: false,
              manual_workflow_status: workflow,
            };
        try {
          const updated = await updateConversationCRM(conversation.id, payload);
          mergeConversation(updated);
          await selectItem("conversation", conversation.id);
        } catch (error) {
          alert(error.message);
        }
      });

      document.getElementById("aiRefreshButton").addEventListener("click", () => {
        generateReplyDraft("conversation", conversation.id).catch((error) => {
          alert(error.message);
        });
      });

      document.getElementById("replyAnchorButton").addEventListener("click", () => {
        generateReplyDraft("conversation", conversation.id).catch((error) => {
          alert(error.message);
        });
      });

      const draftField = document.getElementById("replyDraftField");
      if (draftField) {
        draftField.addEventListener("input", () => {
          state.draft = {
            itemKey: itemKey("conversation", conversation.id),
            text: draftField.value,
            loading: false,
            visible: true,
            note: "",
          };
        });
        draftField.addEventListener("keydown", async (event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            document.getElementById("sendReplyButton")?.click();
          }
        });
      }

      const copyButton = document.getElementById("copyReplyButton");
      if (copyButton) {
        copyButton.addEventListener("click", async () => {
          const field = document.getElementById("replyDraftField");
          if (!field) return;
          try {
            await navigator.clipboard.writeText(field.value);
            copyButton.textContent = "Copié";
            window.setTimeout(() => { copyButton.textContent = "Copier"; }, 1200);
          } catch (error) {
            alert("Impossible de copier le brouillon.");
          }
        });
      }

      const sendButton = document.getElementById("sendReplyButton");
      if (sendButton) {
        sendButton.addEventListener("click", async () => {
          const field = document.getElementById("replyDraftField");
          if (!field || !field.value.trim()) return;
          try {
            await navigator.clipboard.writeText(field.value);
            window.open(`https://www.malt.fr/messages/${encodeURIComponent(conversation.id)}`, "_blank", "noopener,noreferrer");
            sendButton.textContent = "Ouvert dans Malt";
            window.setTimeout(() => { sendButton.textContent = "Copier et ouvrir dans Malt"; }, 1400);
          } catch (error) {
            alert("Impossible d'ouvrir Malt avec le brouillon.");
          }
        });
      }

      if (state.focusDraftOnRender) {
        state.focusDraftOnRender = false;
        window.requestAnimationFrame(() => {
          const card = document.getElementById("draftCard");
          const field = document.getElementById("replyDraftField");
          card?.classList.add("flash");
          window.setTimeout(() => card?.classList.remove("flash"), 1100);
          card?.scrollIntoView({ behavior: "smooth", block: "center" });
          field?.focus();
          if (field && !draftState.loading) {
            field.setSelectionRange(field.value.length, field.value.length);
          }
        });
      }
    }

    function renderOpportunityDetail(body) {
      const opportunity = state.selectedDetail.opportunity;
      const linkedConversation = state.selectedDetail.conversation;
      const status = statusForOpportunity(opportunity);
      const draftState = state.draft.itemKey === itemKey("opportunity", opportunity.id)
        ? state.draft
        : {
            itemKey: itemKey("opportunity", opportunity.id),
            text: opportunity.ai_reply_draft || "",
            loading: false,
            visible: Boolean(opportunity.ai_reply_draft),
            note: "",
          };
      const replyDraft = draftState.text || "";
      const showDraft = draftState.visible || draftState.loading || Boolean(replyDraft);

      document.getElementById("detailTitle").textContent = "Opportunité";
      document.getElementById("detailTime").textContent = "Réponse et contexte";
      const fitScore = opportunity.ai_fit_score != null ? Math.round(opportunity.ai_fit_score) : null;
      const fitLabel = opportunity.ai_fit_label ? opportunity.ai_fit_label.replaceAll("_", " ") : null;
      const strengthBlock = renderStrengthInsightBlock(opportunity.strength, "opp");

      body.innerHTML = `
        <div class="detail-toolbar">
          <div class="detail-meta">
            <div class="detail-heading">
              <h3>${escapeHtml(opportunity.title)}</h3>
              <p>${escapeHtml(formatDate(opportunity.updated_at))}</p>
              <span class="badge ${escapeHtml(status.css)}">${escapeHtml(status.label)}</span>
              ${fitScore != null ? `<span class="badge fit" title="Adéquation IA (voir détail score ci-dessous)">Fit ${escapeHtml(fitScore)}${fitLabel ? ` · ${escapeHtml(fitLabel)}` : ""}</span>` : ""}
            </div>
            <div class="toolbar-actions">
              <button id="replyAnchorButton" type="button" class="primary-button" ${draftState.loading ? "disabled" : ""}>${draftState.loading ? "Génération..." : "Réponse rapide IA"}</button>
              <a class="detail-link" href="https://www.malt.fr/messages/client-project-offer/${encodeURIComponent(opportunity.id)}" target="_blank" rel="noopener noreferrer">Ouvrir Malt</a>
              <button id="archiveOpportunityButton" type="button" class="danger-button">${opportunity.archived_at ? "Désarchiver" : "Archiver"}</button>
            </div>
          </div>
          <div class="crm-insight">${strengthBlock}</div>
          ${showDraft ? `
            <div id="draftCard" class="draft">
              <div class="draft-head">
                <h3>Réponse suggérée</h3>
                <span class="draft-status">${draftState.loading ? "Génération..." : (replyDraft.trim() ? "Prêt à envoyer" : "Aucune réponse suggérée")}</span>
              </div>
              ${draftState.note ? `<div class="draft-hint">${escapeHtml(draftState.note)}</div>` : ""}
              <textarea id="replyDraftField" ${draftState.loading ? "disabled" : ""} placeholder="La réponse IA apparaîtra ici.">${escapeHtml(replyDraft)}</textarea>
              <div class="draft-actions">
                <span class="draft-hint">Copier et ouvrir dans Malt.</span>
                <div class="draft-buttons">
                  <button id="sendReplyButton" type="button" class="primary-button" ${draftState.loading || !replyDraft.trim() ? "disabled" : ""}>Copier et ouvrir dans Malt</button>
                  <button id="copyReplyButton" type="button" class="ghost-button" ${draftState.loading || !replyDraft.trim() ? "disabled" : ""}>Copier</button>
                </div>
              </div>
            </div>
          ` : ""}
        </div>
        <p class="detail-summary">${escapeHtml(summaryForOpportunity(opportunity))}</p>
        <div class="thread">
          ${opportunity.budget ? `<article class="message"><div class="message-meta"><span>Budget</span></div><div class="message-body">${renderRichText(`${opportunity.budget} €`)}</div></article>` : ""}
          ${opportunity.description ? `<article class="message"><div class="message-meta"><span>Description</span></div><div class="message-body">${renderRichText(opportunity.description)}</div></article>` : ""}
          ${linkedConversation ? `<article class="message"><div class="message-meta"><span>Conversation liée</span></div><div class="message-body">${renderRichText(linkedConversation.client_name)}</div></article>` : ""}
        </div>
      `;

      body.scrollTop = 0;
      document.getElementById("archiveOpportunityButton").addEventListener("click", async () => {
        try {
          const updated = await updateOpportunityCRM(opportunity.id, {
            archived: !opportunity.archived_at,
          });
          mergeOpportunity(updated);
          state.selectedDetail.opportunity = updated;
          render();
        } catch (error) {
          alert(error.message);
        }
      });
      document.getElementById("replyAnchorButton").addEventListener("click", () => {
        generateReplyDraft("opportunity", opportunity.id).catch((error) => {
          alert(error.message);
        });
      });

      const draftField = document.getElementById("replyDraftField");
      if (draftField) {
        draftField.addEventListener("input", () => {
          state.draft = {
            itemKey: itemKey("opportunity", opportunity.id),
            text: draftField.value,
            loading: false,
            visible: true,
            note: "",
          };
        });
        draftField.addEventListener("keydown", async (event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            document.getElementById("sendReplyButton")?.click();
          }
        });
      }

      const copyButton = document.getElementById("copyReplyButton");
      if (copyButton) {
        copyButton.addEventListener("click", async () => {
          const field = document.getElementById("replyDraftField");
          if (!field) return;
          await navigator.clipboard.writeText(field.value);
          copyButton.textContent = "Copié";
          window.setTimeout(() => { copyButton.textContent = "Copier"; }, 1200);
        });
      }

      const sendButton = document.getElementById("sendReplyButton");
      if (sendButton) {
        sendButton.addEventListener("click", async () => {
          const field = document.getElementById("replyDraftField");
          if (!field || !field.value.trim()) return;
          await navigator.clipboard.writeText(field.value);
          window.open(`https://www.malt.fr/messages/client-project-offer/${encodeURIComponent(opportunity.id)}`, "_blank", "noopener,noreferrer");
          sendButton.textContent = "Ouvert dans Malt";
          window.setTimeout(() => { sendButton.textContent = "Copier et ouvrir dans Malt"; }, 1400);
        });
      }

      if (state.focusDraftOnRender) {
        state.focusDraftOnRender = false;
        window.requestAnimationFrame(() => {
          const card = document.getElementById("draftCard");
          const field = document.getElementById("replyDraftField");
          card?.classList.add("flash");
          window.setTimeout(() => card?.classList.remove("flash"), 1100);
          card?.scrollIntoView({ behavior: "smooth", block: "center" });
          field?.focus();
          if (field && !draftState.loading) {
            field.setSelectionRange(field.value.length, field.value.length);
          }
        });
      }
    }

    function render() {
      renderList();
      renderDetail();
    }

    function mergeConversation(updatedConversation) {
      state.conversations = state.conversations
        .map((item) => item.id === updatedConversation.id ? { ...item, ...updatedConversation } : item)
        .sort((left, right) => new Date(right.updated_at) - new Date(left.updated_at));
    }

    function mergeOpportunity(updatedOpportunity) {
      state.opportunities = state.opportunities
        .map((item) => item.id === updatedOpportunity.id ? { ...item, ...updatedOpportunity } : item)
        .sort((left, right) => new Date(right.updated_at) - new Date(left.updated_at));
    }

    function updateOpportunityCRM(opportunityId, payload) {
      return fetchJson(`/api/opportunities/${encodeURIComponent(opportunityId)}/crm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }

    function updateConversationCRM(conversationId, payload) {
      return fetchJson(`/api/conversations/${encodeURIComponent(conversationId)}/crm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }

    function postConversationQuickAction(conversationId, action) {
      return fetchJson(`/api/conversations/${encodeURIComponent(conversationId)}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
    }

    function loadConversations() {
      return fetchJson("/api/conversations?limit=300").then((rows) => {
        state.conversations = [...rows].sort((left, right) => new Date(right.updated_at) - new Date(left.updated_at));
      });
    }

    function loadOpportunities() {
      return fetchJson("/api/opportunities").then((rows) => {
        state.opportunities = [...rows].sort((left, right) => new Date(right.updated_at) - new Date(left.updated_at));
      });
    }

    function refreshStatus() {
      return fetchJson("/api/status").then(renderStatus);
    }

    function syncDraftFromConversation(conversation) {
      state.draft = {
        itemKey: itemKey("conversation", conversation.id),
        text: conversation.ai_reply_draft || "",
        loading: false,
        visible: Boolean(conversation.ai_reply_draft),
        note: "",
      };
    }

    function syncDraftFromOpportunity(opportunity) {
      state.draft = {
        itemKey: itemKey("opportunity", opportunity.id),
        text: opportunity.ai_reply_draft || "",
        loading: false,
        visible: Boolean(opportunity.ai_reply_draft),
        note: "",
      };
    }

    async function generateReplyDraft(kind, id) {
      if (!state.selectedDetail || state.selectedDetail.kind !== kind || (kind === "conversation" ? state.selectedDetail.conversation.id : state.selectedDetail.opportunity.id) !== id) {
        await selectItem(kind, id);
      }

      state.draft = {
        itemKey: itemKey(kind, id),
        text: "",
        loading: true,
        visible: true,
        note: "",
      };
      state.focusDraftOnRender = true;
      renderDetail();

      const [payload] = await Promise.all([
        fetchJson(
          kind === "conversation"
            ? `/api/conversations/${encodeURIComponent(id)}/ai-refresh`
            : `/api/opportunities/${encodeURIComponent(id)}/ai-draft`,
          { method: "POST" }
        ),
        new Promise((resolve) => window.setTimeout(resolve, 450)),
      ]);
      if (kind === "conversation") {
        mergeConversation(payload.conversation);
        state.selectedDetail = { kind: "conversation", ...payload };
        state.draft = {
          itemKey: itemKey(kind, id),
          text: payload.conversation.ai_reply_draft || "",
          loading: false,
          visible: true,
          note: payload.conversation.ai_reply_draft
            ? ""
            : (payload.conversation.next_action || "Aucune réponse suggérée pour le moment."),
        };
      } else {
        mergeOpportunity(payload.opportunity);
        state.selectedDetail = { kind: "opportunity", ...payload };
        state.draft = {
          itemKey: itemKey(kind, id),
          text: payload.opportunity.ai_reply_draft || "",
          loading: false,
          visible: true,
          note: payload.opportunity.ai_reply_draft
            ? ""
            : (payload.opportunity.ai_summary || "Aucune réponse suggérée pour le moment."),
        };
      }
      state.focusDraftOnRender = true;
      render();
    }

    function selectItem(kind, id) {
      if (!kind || !id) return Promise.resolve();
      state.selectedItem = { kind, id };
      const url = kind === "conversation"
        ? `/api/conversations/${encodeURIComponent(id)}`
        : `/api/opportunities/${encodeURIComponent(id)}`;
      return fetchJson(url).then((payload) => {
        state.selectedDetail = { kind, ...payload };
        if (kind === "conversation") {
          syncDraftFromConversation(payload.conversation);
        } else {
          syncDraftFromOpportunity(payload.opportunity);
        }
        render();
      });
    }

    function fullRefresh() {
      return Promise.all([refreshStatus(), loadConversations(), loadOpportunities()]).then(async () => {
        const items = feedItems();
        if (state.selectedItem) {
          await selectItem(state.selectedItem.kind, state.selectedItem.id);
        } else if (items.length) {
          await selectItem(items[0].kind, items[0].id);
        } else {
          render();
        }
      });
    }

    document.getElementById("syncButton").addEventListener("click", async () => {
      try {
        await fetchJson("/api/sync", { method: "POST" });
        state.syncRefreshPending = true;
        await refreshStatus();
      } catch (error) {
        alert(error.message);
      }
    });

    document.getElementById("activeViewButton").addEventListener("click", async () => {
      state.listView = "active";
      document.getElementById("activeViewButton").classList.add("active");
      document.getElementById("archivedViewButton").classList.remove("active");
      const items = feedItems();
      if (state.selectedItem && !items.find((item) => item.kind === state.selectedItem.kind && item.id === state.selectedItem.id)) {
        state.selectedItem = items[0] ? { kind: items[0].kind, id: items[0].id } : null;
      }
      if (state.selectedItem) {
        await selectItem(state.selectedItem.kind, state.selectedItem.id);
      } else {
        render();
      }
    });

    document.getElementById("archivedViewButton").addEventListener("click", async () => {
      state.listView = "archived";
      document.getElementById("archivedViewButton").classList.add("active");
      document.getElementById("activeViewButton").classList.remove("active");
      const items = feedItems();
      if (state.selectedItem && !items.find((item) => item.kind === state.selectedItem.kind && item.id === state.selectedItem.id)) {
        state.selectedItem = items[0] ? { kind: items[0].kind, id: items[0].id } : null;
      }
      if (state.selectedItem) {
        await selectItem(state.selectedItem.kind, state.selectedItem.id);
      } else {
        render();
      }
    });

    document.addEventListener("keydown", async (event) => {
      const target = event.target;
      const tagName = target?.tagName || "";
      if (tagName === "TEXTAREA" || tagName === "INPUT" || tagName === "SELECT") {
        return;
      }
      const items = feedItems();
      if (!items.length) {
        return;
      }
      const currentIndex = Math.max(0, items.findIndex((item) => state.selectedItem && item.kind === state.selectedItem.kind && item.id === state.selectedItem.id));
      if (event.key === "ArrowDown") {
        event.preventDefault();
        const nextIndex = Math.min(items.length - 1, currentIndex + 1);
        await selectItem(items[nextIndex].kind, items[nextIndex].id);
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        const nextIndex = Math.max(0, currentIndex - 1);
        await selectItem(items[nextIndex].kind, items[nextIndex].id);
      }
      if (event.key === "Enter" && state.selectedItem) {
        event.preventDefault();
        document.getElementById("detailBody")?.focus?.();
        document.getElementById("detailBody")?.scrollTo({ top: 0, behavior: "smooth" });
      }
    });

    fullRefresh().catch(console.error);
    window.setInterval(refreshStatus, 10000);
    window.setInterval(fullRefresh, 30000);
