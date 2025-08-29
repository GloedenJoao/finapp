// Confirmação e POST para deletar, depois redireciona para /transactions
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tx-delete-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = form.getAttribute("data-id") || "";
      const ok = window.confirm(Excluir o lançamento ${id}?);
      if (!ok) return;

      try {
        const resp = await fetch(form.action, {
          method: "POST",
          headers: { "Accept": "text/html" }
        });

        // O FastAPI responde com RedirectResponse(303) -> fetch segue o redirect,
        // mas não troca a URL do navegador. Forçamos o redirect no client:
        if (resp.ok) {
          window.location.href = "/transactions";
        } else {
          // fallback: tenta mesmo assim
          window.location.href = "/transactions";
        }
      } catch (err) {
        console.error(err);
        window.location.href = "/transactions";
      }
    });
  });
});