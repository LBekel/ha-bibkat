const CARD_TYPE = "bibkat-loans-card";

const TEXT = {
  de: {
    title: "Bibkat-Ausleihen",
    unavailable: "Ausleihen sind derzeit nicht verfügbar.",
    empty: "Keine Medien ausgeliehen.",
    due: "Rückgabe",
    renewable: "Verlängerbar",
    notRenewable: "Nicht verlängerbar",
    pending: "Anfrage offen",
    renew: "Verlängern",
    renewing: "Wird angefragt …",
    sent: "Verlängerungsanfrage gesendet.",
    failed: "Anfrage fehlgeschlagen. Prüfe Bibkat, bevor du es erneut versuchst.",
    confirm: (title) => `Möchtest du „${title}“ jetzt verlängern lassen?`,
    entity: "Ausleihen-Sensor",
    account: "Bibkat-Leserkonto",
  },
  en: {
    title: "Bibkat loans",
    unavailable: "Loans are currently unavailable.",
    empty: "No items currently borrowed.",
    due: "Due",
    renewable: "Renewable",
    notRenewable: "Not renewable",
    pending: "Request pending",
    renew: "Renew",
    renewing: "Requesting …",
    sent: "Renewal request sent.",
    failed: "Request failed. Check Bibkat before trying again.",
    confirm: (title) => `Request a renewal for "${title}" now?`,
    entity: "Loan list sensor",
    account: "Bibkat reader account",
  },
};

class BibkatLoansCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._busy = new Set();
    this._messages = new Map();
  }

  setConfig(config) {
    if (!config.entity || !config.config_entry_id) {
      throw new Error(
        "Set both entity and config_entry_id for the Bibkat loans card.",
      );
    }
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 3;
  }

  static getConfigElement() {
    return document.createElement("bibkat-loans-card-editor");
  }

  static getStubConfig() {
    return { entity: "", config_entry_id: "" };
  }

  _render() {
    if (!this._config || !this._hass) return;

    const language = this._hass.locale?.language?.startsWith("de")
      ? TEXT.de
      : TEXT.en;
    const card = document.createElement("ha-card");
    const heading = document.createElement("div");
    heading.className = "heading";
    heading.textContent = this._config.title || language.title;
    card.append(heading);

    const content = document.createElement("div");
    content.className = "content";
    const entity = this._hass.states[this._config.entity];
    const loans = entity?.attributes?.loans;
    if (!entity || entity.state === "unavailable" || !Array.isArray(loans)) {
      content.textContent = language.unavailable;
    } else if (loans.length === 0) {
      content.textContent = language.empty;
    } else {
      const list = document.createElement("div");
      list.className = "loans";
      for (const loan of loans) {
        list.append(this._renderLoan(loan, language));
      }
      content.append(list);
    }
    card.append(content);

    this.shadowRoot.replaceChildren();
    this.shadowRoot.append(card);
    const style = document.createElement("style");
    style.textContent = `
      ha-card { overflow: hidden; }
      .heading { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; }
      .content { padding: 12px 16px 16px; }
      .loans { display: grid; gap: 8px; }
      .loan { display: grid; grid-template-columns: minmax(0, 1fr) auto;
        align-items: center; gap: 12px; border-bottom: 1px solid var(--divider-color);
        padding: 8px 0; }
      .loan:last-child { border-bottom: 0; }
      .title { font-weight: 500; overflow-wrap: anywhere; }
      .details { color: var(--secondary-text-color); font-size: 0.9em; margin-top: 4px; }
      .status { color: var(--secondary-text-color); font-size: 0.85em; margin-top: 4px; }
      button { background: var(--primary-color); border: 0; border-radius: 4px;
        color: var(--text-primary-color); cursor: pointer; font: inherit;
        min-height: 36px; padding: 0 12px; }
      button:disabled { cursor: default; opacity: 0.5; }
      .message { color: var(--secondary-text-color); font-size: 0.85em; grid-column: 1 / -1; }
      @media (max-width: 420px) {
        .loan { grid-template-columns: 1fr; }
        button { justify-self: start; }
      }
    `;
    this.shadowRoot.append(style);
  }

  _renderLoan(loan, language) {
    const row = document.createElement("div");
    row.className = "loan";

    const description = document.createElement("div");
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = loan.title || loan.medium_id || "";
    description.append(title);

    const details = document.createElement("div");
    details.className = "details";
    details.textContent = `${language.due}: ${loan.due_date || "—"}`;
    description.append(details);

    const status = document.createElement("div");
    status.className = "status";
    status.textContent = loan.is_pending
      ? language.pending
      : loan.is_renewable
        ? language.renewable
        : language.notRenewable;
    description.append(status);
    row.append(description);

    const button = document.createElement("button");
    const busy = this._busy.has(loan.medium_id);
    button.textContent = busy ? language.renewing : language.renew;
    button.disabled =
      busy || loan.is_pending !== false || loan.is_renewable !== true;
    button.addEventListener("click", () => this._renew(loan, language));
    row.append(button);

    const message = this._messages.get(loan.medium_id);
    if (message) {
      const messageElement = document.createElement("div");
      messageElement.className = "message";
      messageElement.textContent = message;
      row.append(messageElement);
    }
    return row;
  }

  async _renew(loan, language) {
    if (
      typeof loan.medium_id !== "string" ||
      !/^[1-9][0-9]{0,15}$/.test(loan.medium_id) ||
      !/^\d{4}-\d{2}-\d{2}$/.test(loan.due_date || "")
    ) {
      this._messages.set(loan.medium_id, language.failed);
      this._render();
      return;
    }
    if (!window.confirm(language.confirm(loan.title || loan.medium_id))) return;

    this._busy.add(loan.medium_id);
    this._messages.delete(loan.medium_id);
    this._render();
    try {
      await this._hass.callService("bibkat", "renew", {
        config_entry_id: this._config.config_entry_id,
        medium_id: loan.medium_id,
        expected_due_date: loan.due_date,
      });
      this._messages.set(loan.medium_id, language.sent);
    } catch (error) {
      this._messages.set(
        loan.medium_id,
        error?.message ? `${language.failed} ${error.message}` : language.failed,
      );
    } finally {
      this._busy.delete(loan.medium_id);
      this._render();
    }
  }
}

class BibkatLoansCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass) return;
    const language = this._hass.locale?.language?.startsWith("de")
      ? TEXT.de
      : TEXT.en;
    this.replaceChildren();
    this._addSelector(language.entity, "entity", {
      entity: { filter: [{ integration: "bibkat", domain: "sensor" }] },
    });
    this._addSelector(language.account, "config_entry_id", {
      config_entry: { integration: "bibkat" },
    });
  }

  _addSelector(labelText, key, selector) {
    const wrapper = document.createElement("div");
    wrapper.style.marginBottom = "16px";
    const label = document.createElement("div");
    label.textContent = labelText;
    label.style.marginBottom = "8px";
    const input = document.createElement("ha-selector");
    input.hass = this._hass;
    input.selector = selector;
    input.value = this._config[key] || "";
    input.addEventListener("value-changed", (event) => {
      this._config = { ...this._config, [key]: event.detail.value };
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          bubbles: true,
          composed: true,
          detail: { config: this._config },
        }),
      );
    });
    wrapper.append(label, input);
    this.append(wrapper);
  }
}

customElements.define(CARD_TYPE, BibkatLoansCard);
customElements.define("bibkat-loans-card-editor", BibkatLoansCardEditor);
window.customCards = window.customCards || [];
window.customCards.push({
  type: CARD_TYPE,
  name: "Bibkat loans",
  description: "List Bibkat loans and request eligible renewals.",
});
