import { ImageView } from "../../../../libs/frontend-test/src/helpers/LSF/ImageView";

type Identity = "admin" | "qa" | "alice" | "bob" | "charlie";
type CampaignFixture = {
  project: number;
  password: string;
  task_ids: number[];
  users: Record<Identity, { id: number; email: string }>;
};

describe("Collaborative campaign", () => {
  let fixture: CampaignFixture;
  let task: number;
  let annotation: number;
  let correctedResult: unknown[];
  let failed = false;
  const correction = "Browser acceptance: correct the document category";

  function login(name: Identity) {
    // Stop the previous application before switching accounts. Its background
    // requests must not repopulate the cookie jar with the previous session.
    cy.visit("/logout/", { log: false });
    cy.clearAllCookies();
    cy.clearAllLocalStorage();
    cy.request({ url: "/user/login/", log: false });
    cy.getCookie("csrftoken", { log: false }).then((cookie) => {
      expect(cookie, "login CSRF cookie").not.to.be.null;
      cy.request({
        method: "POST", url: "/user/login/", form: true, followRedirect: false, log: false,
        body: { email: fixture.users[name].email, password: fixture.password, csrfmiddlewaretoken: cookie!.value },
        headers: { Referer: `${Cypress.config("baseUrl")}/user/login/` },
      }).its("status").should("eq", 302);
    });
    cy.request({ url: "/api/current-user/whoami", log: false }).its("body.id").should("eq", fixture.users[name].id);
  }

  function api(method: string, url: string, body?: Cypress.RequestBody, failOnStatusCode = true) {
    return cy.getCookie("csrftoken", { log: false }).then((cookie) => cy.request({
      method, url, body, failOnStatusCode, log: false,
      headers: { "X-CSRFToken": cookie?.value ?? "", Referer: `${Cypress.config("baseUrl")}/` },
    }));
  }

  function openAssignment() {
    cy.intercept("GET", "**/api/extensions/v1/my-assignments*").as("assignments");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("h1", "Collaborative acceptance").should("be.visible");
    cy.wait("@assignments");
    function findPage(remaining = 5) {
      cy.get(".lsf-campaign-table-wrap tbody tr").then(($rows) => {
        const target = $rows.filter((_, row) => row.querySelector("td")?.textContent?.trim() === `#${task}`);
        if (target.length) {
          cy.wrap(target).contains("button", "Annotate").click();
        } else {
          expect(remaining, "assignment exists in the paginated queue").to.be.greaterThan(0);
          cy.get(".lsf-campaign-pagination").first().find("span").invoke("text").then((previous) => {
            cy.get(".lsf-campaign-pagination").first().contains("button", "Next").should("not.be.disabled").click();
            cy.wait("@assignments");
            cy.get(".lsf-campaign-pagination").first().find("span").should("not.have.text", previous);
            findPage(remaining - 1);
          });
        }
      });
    }
    findPage();
    cy.get(".lsf-choices", { timeout: 60000 }).should("be.visible");
  }

  function choose(value: string) {
    cy.get(".lsf-choices").find(`.lsf-choice__item input[name="${value}"]`).check();
    cy.get(".lsf-choices").find(`.lsf-choice__item input[name="${value}"]`).should("be.checked");
  }

  function waitForMultimodalDraft(remaining = 40) {
    return api("GET", `/api/tasks/${task}/?project=${fixture.project}`).then(({ body }) => {
      const saved = (body.drafts ?? []).some((draft: { result: { type: string }[] }) =>
        ["choices", "labels", "rectanglelabels"].every((type) => draft.result?.some((item) => item.type === type)));
      if (saved) return;
      expect(remaining, "automatic draft contains the text and image edits").to.be.greaterThan(0);
      cy.wait(250, { log: false });
      return waitForMultimodalDraft(remaining - 1);
    });
  }

  function selectFreshTask(candidates: number[]) {
    const [candidate, ...remaining] = candidates;
    expect(candidate, "an untouched source task remains").to.be.a("number");
    return api("GET", `/api/tasks/${candidate}/?project=${fixture.project}`).then(({ body }) => {
      if ((body.drafts ?? []).length) return selectFreshTask(remaining);
      task = candidate;
    });
  }

  function assertEditorResult(type: string) {
    cy.window().should((window) => {
      const store = (window as any).Htx;
      expect(Number(store.task.id), "native editor task").to.eq(task);
      expect(store.annotationStore.selected.serializeAnnotation().map((item: { type: string }) => item.type), "native editor results").to.include(type);
    });
  }

  function openReview() {
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("button", `Compare #${task}`).click();
    cy.contains("h2", `Compare task #${task}`).should("be.visible");
    cy.get(".lsf-campaign-comparison > article").should("have.length", 3);
  }

  function ownReviewCard() {
    return cy.contains(".lsf-campaign-comparison article > p", `Annotation #${annotation}`).closest("article");
  }

  function assertSavedCanvas(root: () => Cypress.Chainable<JQuery<HTMLElement>>, category: string) {
    root().find(`.lsf-choices input[name="${category}"]`).should("be.checked");
    root().find(".lsf-htx-richtext").first().scrollIntoView().should("be.visible").should(($text) => {
      const element = $text[0].tagName === "IFRAME" ? ($text[0] as HTMLIFrameElement).contentDocument?.body : $text[0];
      const highlights = Array.from(element?.querySelectorAll(".htx-highlight") ?? []);
      expect(highlights.some((highlight) => highlight.textContent?.includes("Alice Example")), "saved NER region is visible").to.eq(true);
    });
    root().find(".lsf-outliner-item").contains("Stamp").scrollIntoView().should("be.visible");
  }

  before(() => {
    cy.task<CampaignFixture>("campaignFixture", null, { log: false }).then((value) => {
      fixture = value;
      task = fixture.task_ids[1];
      login("admin");
      api("POST", `/api/extensions/v1/projects/${fixture.project}/restore`, {}).its("status").should("eq", 200);
      login("qa");
      api("GET", `/api/extensions/v1/projects/${fixture.project}/results`).then(({ body }) => {
        const unused = body.results.filter((item: { task: number; annotations: unknown[] }) => item.annotations.length === 0);
        expect(unused.length, "an untouched task for repeatable browser acceptance").to.be.greaterThan(0);
        login("alice");
        selectFreshTask(unused.map((item: { task: number }) => item.task));
      });
    });
  });

  beforeEach(function () {
    if (failed) this.skip();
  });

  afterEach(function () {
    failed ||= this.currentTest?.state === "failed";
  });

  it("annotates text and image as Alice", () => {
    login("alice");
    openAssignment();
    cy.contains("button", "Administration").should("not.exist");
    choose("a");
    assertEditorResult("choices");
    cy.get(".lsf-label").contains("Stamp").click();
    cy.get(".lsf-label_selected").contains("Stamp").should("be.visible");
    cy.get("img[alt=image]").should(($image) => expect(($image[0] as HTMLImageElement).naturalWidth).to.eq(640));
    cy.get('[class^="image-element--"] .konvajs-content canvas').should("be.visible");
    ImageView.drawRectRelative(0.65, 0.55, 0.25, 0.25);
    assertEditorResult("rectanglelabels");
    cy.get(".lsf-label").contains("Name").click();
    cy.get(".lsf-htx-richtext").first().should(($text) => {
      const root = $text[0];
      const element = root.tagName === "IFRAME" ? (root as HTMLIFrameElement).contentDocument?.body : root;
      expect(element?.textContent).to.contain("Alice Example");
    }).then(($text) => {
      const root = $text[0];
      const element = root.tagName === "IFRAME" ? (root as HTMLIFrameElement).contentDocument!.body : root;
      const document = element.ownerDocument;
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node && !node.textContent?.includes("Alice Example")) node = walker.nextNode();
      expect(node, "source text node").not.to.be.null;
      const range = document.createRange();
      const start = node!.textContent!.indexOf("Alice Example");
      range.setStart(node!, start);
      range.setEnd(node!, start + 13);
      cy.wrap(element).trigger("mousedown", { eventConstructor: "MouseEvent", buttons: 1 });
      cy.wrap(element).then(() => {
        document.getSelection()!.removeAllRanges();
        document.getSelection()!.addRange(range);
      });
      cy.wrap(element).trigger("mouseup", { eventConstructor: "MouseEvent", buttons: 1 });
    });
    waitForMultimodalDraft();
    cy.reload();
    cy.get('.lsf-choices input[name="a"]', { timeout: 60000 }).should("be.checked");
    cy.screenshot("campaign-native-text-image");
    cy.intercept({ method: "POST", pathname: new RegExp(`^/api/tasks/${task}/annotations/?$`) }).as("submit");
    cy.get('[data-testid="bottombar-submit-button"]').click();
    cy.wait("@submit").then(({ response }) => {
      expect(response?.statusCode).to.eq(201);
      annotation = response!.body.id;
      const types = response!.body.result.map((item: { type: string }) => item.type);
      expect(types).to.include.members(["choices", "labels", "rectanglelabels"]);
    });
  });

  it("requests an anonymous correction", () => {
    for (const [name, choice] of [["bob", "b"], ["charlie", "c"]] as const) {
      login(name);
      api("POST", `/api/tasks/${task}/annotations/`, {
        result: [{ from_name: "category", to_name: "text", type: "choices", value: { choices: [choice] } }],
      }).its("status").should("eq", 201);
    }
    login("qa");
    openReview();
    assertSavedCanvas(ownReviewCard, "a");
    cy.get('.lsf-campaign-comparison > article').eq(1).find('.lsf-choices input[name="b"]').should("be.checked");
    cy.get('.lsf-campaign-comparison > article').eq(2).find('.lsf-choices input[name="c"]').should("be.checked");
    cy.get(".lsf-campaign-comparison > article > h3").each(($heading) => expect($heading.text()).to.include("Annotateur"));
    cy.get(".lsf-campaign-comparison").should("not.contain", fixture.users.alice.email);
    cy.get('textarea[placeholder="Required for rejection or correction"]').type(correction);
    cy.intercept("POST", "/api/extensions/v1/reviews").as("review");
    ownReviewCard().contains("button", "Request correction").click();
    cy.wait("@review").then(({ request, response }) => {
      expect(request.body.revision).to.eq(1);
      expect(response?.statusCode).to.eq(201);
    });
    cy.screenshot("campaign-anonymous-comparison");
  });

  it("resubmits private feedback with its revision", () => {
    login("bob");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.get(".lsf-campaign").should("not.contain", correction);
    api("GET", `/api/annotations/${annotation}/`, undefined, false).its("status").should("be.oneOf", [403, 404]);
    login("alice");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains(correction).should("be.visible");
    openAssignment();
    choose("b");
    cy.intercept({ method: "PATCH", pathname: new RegExp(`^/api/annotations/${annotation}/?$`) }).as("correct");
    cy.get('[data-testid="bottombar-update-button"]').click();
    cy.wait("@correct").then(({ request, response }) => {
      expect(request.body.revision).to.eq(1);
      expect(response?.statusCode).to.eq(200);
      expect(response?.body.revision).to.eq(2);
      correctedResult = response!.body.result;
      expect((correctedResult as { type: string }[]).map((item) => item.type)).to.include.members(["choices", "labels", "rectanglelabels"]);
    });
  });

  it("arbitrates without overwriting originals", () => {
    login("qa");
    openReview();
    assertSavedCanvas(ownReviewCard, "b");
    cy.intercept("POST", "/api/extensions/v1/reviews").as("accept");
    ownReviewCard().contains("button", "Accept").click();
    cy.wait("@accept").its("response.statusCode").should("eq", 201);
    api("GET", `/api/extensions/v1/tasks/${task}/review`).then(({ body }) => {
      for (const item of body.annotations.filter((entry: { review_status: string }) => entry.review_status === "pending")) {
        api("POST", "/api/extensions/v1/reviews", { annotation: item.id, revision: item.revision, decision: "ACCEPT" });
      }
    });
    ownReviewCard().contains("button", "Arbitrate from this annotation").click();
    cy.get(".lsf-campaign-final .lsf-choices").should("be.visible");
    assertSavedCanvas(() => cy.get(".lsf-campaign-final"), "b");
    cy.get('.lsf-campaign-final [data-testid="bottombar-submit-button"]').should("not.exist");
    cy.intercept("POST", `/api/extensions/v1/tasks/${task}/final`).as("arbitrate");
    cy.contains("button", "Save final annotation").click();
    cy.wait("@arbitrate").then(({ request, response }) => {
      expect(response?.statusCode).to.eq(201);
      expect(request.body.result).to.have.length(3);
      expect(request.body.result).to.have.deep.members(correctedResult);
    });
    api("GET", `/api/extensions/v1/tasks/${task}/review`).then(({ body }) => {
      expect(body.annotations).to.have.length(3);
      expect(body.arbitration.final_annotation).to.be.a("number");
      expect(body.annotations.find((item: { id: number }) => item.id === annotation).revisions).to.have.length(1);
      api("GET", `/api/annotations/${body.arbitration.final_annotation}/`).then(({ body: final }) => {
        expect(final.result).to.have.length(3);
        expect(final.result).to.have.deep.members(correctedResult);
      });
    });
  });

  it("archives and restores from Administration", () => {
    login("admin");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("button", "Administration").click();
    cy.contains("button", "Archive").click();
    cy.contains("This campaign is archived.").should("be.visible");
    login("alice");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("This campaign is archived.").should("be.visible");
    cy.contains("button", "Annotate").should("not.exist");
    login("admin");
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("button", "Administration").click();
    cy.contains("button", "Restore").click();
    cy.contains("This campaign is archived.").should("not.exist");
    cy.screenshot("campaign-administration");
  });
});
