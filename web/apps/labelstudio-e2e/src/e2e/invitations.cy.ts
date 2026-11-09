describe("Email invitations", () => {
  let fixture: any;

  before(() => {
    cy.task("invitationFixture", null, { log: false }).then((value) => { fixture = value; });
  });

  function login(email: string) {
    cy.request({ url: "/user/login/", log: false });
    cy.getCookie("csrftoken", { log: false }).then((cookie) => {
      cy.request({ method: "POST", url: "/user/login/", form: true, followRedirect: false, log: false,
        body: { email, password: fixture.password, csrfmiddlewaretoken: cookie!.value },
        headers: { Referer: `${Cypress.config("baseUrl")}/user/login/` },
      }).its("status").should("eq", 302);
    });
  }

  function assertTask() {
    cy.contains("a", "Ouvrir mes tâches").click();
    cy.contains("h1", "Invitation acceptance").should("be.visible");
    cy.contains("td", `#${fixture.task}`).should("be.visible");
    cy.contains("button", "Annotate").should("be.visible");
    cy.request({ url: "/api/current-user/whoami", log: false }).its("body.email").should("match", /-alice@example\.test$|-bob@example\.test$/);
  }

  it("activates a new account from the email fragment and finds its reserved task", () => {
    cy.intercept("POST", "**/user/invitation/").as("activation");
    cy.visit(fixture.links.alice, { log: false });
    cy.location("hash").should("eq", "");
    cy.get("#id_token").invoke("val").should("not.be.empty");
    cy.get("#id_password").type(fixture.password, { log: false });
    cy.get("#id_confirmation").type(fixture.password, { log: false });
    cy.contains("button", "Activer mon accès").click();
    cy.wait("@activation").then(({ request, response }) => {
      expect(request.headers.origin, "same-origin form request").to.eq(Cypress.config("baseUrl"));
      expect(response?.statusCode).to.eq(200);
    });
    cy.contains("Votre accès est activé").should("be.visible");
    assertTask();
  });

  it("requires the matching existing account and preserves its link through sign-in", () => {
    login(fixture.admin);
    cy.visit(fixture.links.bob, { log: false });
    cy.contains("button", "Activer mon accès").click();
    cy.get('[role="alert"]').should("contain.text", "compte invité");
    cy.contains("button", "Se déconnecter").click();
    cy.get("#id_token").invoke("val").should("not.be.empty");
    cy.contains("a", "Connectez-vous avant d’accepter").click();
    cy.get('input[name="email"]').type(fixture.emails.bob, { log: false });
    cy.get('input[name="password"]').type(fixture.password, { log: false });
    cy.get('button[type="submit"]').click();
    cy.location("pathname").should("eq", "/user/invitation/");
    cy.get("#id_token").invoke("val").should("not.be.empty");
    cy.contains("button", "Activer mon accès").click();
    cy.contains("Votre accès est activé").should("be.visible");
    assertTask();
  });

  it("queues invitations from the administrator interface with session CSRF", () => {
    login(fixture.admin);
    cy.visit(`/projects/${fixture.project}/collaboration`);
    cy.contains("button", "Administration").click();
    cy.contains("h2", "Invite annotators by email").should("be.visible");
    cy.get(`input[aria-label="Assign task ${fixture.task}"]`).check();
    cy.contains("label", "Email addresses").find("textarea").type(fixture.admin.replace('-admin@', '-charlie@'), { log: false });
    cy.contains("button", "Invite and pre-assign selected tasks").click();
    cy.contains("Invitations queued").should("be.visible");
  });
});
