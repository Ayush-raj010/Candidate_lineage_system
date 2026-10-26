// Sign-in page. On success the server sets the HttpOnly session cookie; this script
// only redirects to where the user was going (same-site paths only).
import { ApiError, api } from "./api.js";
import { safeNextPath } from "./utils.js";

const $ = (selector) => document.querySelector(selector);
const params = new URLSearchParams(location.search);
const form = $("#login-form");
const errorBox = $("#login-error");
const submit = $("#login-submit");

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function notice() {
  const box = $("#login-notice");
  if (params.has("expired")) box.textContent = "Your session has ended. Sign in again to continue.";
  else if (params.has("signed_out")) box.textContent = "You've been signed out.";
  else return;
  box.hidden = false;
}

function minutes(seconds) {
  const m = Math.ceil(seconds / 60);
  return m <= 1 ? "a minute" : `${m} minutes`;
}

$("#login-show").addEventListener("click", (event) => {
  const input = $("#login-password");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  event.currentTarget.textContent = showing ? "Show" : "Hide";
  event.currentTarget.setAttribute("aria-pressed", String(!showing));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  const email = $("#login-email").value.trim();
  const password = $("#login-password").value;
  if (!email || !password) {
    showError("Enter your email and password.");
    (email ? $("#login-password") : $("#login-email")).focus();
    return;
  }
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    await api.login(email, password);
    location.replace(safeNextPath(params.get("next")));
  } catch (error) {
    let message = error instanceof ApiError ? error.message : "Something went wrong. Try again.";
    if (error instanceof ApiError && error.status === 429 && error.retryAfter) {
      message = `Too many failed attempts. Try again in ${minutes(error.retryAfter)}.`;
    }
    showError(message);
    $("#login-password").value = "";
    $("#login-password").focus();
    submit.disabled = false;
    submit.textContent = "Sign in";
  }
});

const ROLE_LABELS = { admin: "Admin", hiring_manager: "Hiring manager", recruiter: "Recruiter" };

// Listed only when the server runs with GCCX_DEMO_LOGIN on; otherwise the list is empty.
async function demoAccounts() {
  let accounts = [];
  try {
    accounts = (await api.health()).demo_accounts || [];
  } catch {
    return;
  }
  if (!accounts.length) return;
  const list = $("#demo-list");
  for (const account of accounts) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-ghost demo-account";
    const role = document.createElement("strong");
    role.textContent = ROLE_LABELS[account.role] || account.role;
    const detail = document.createElement("span");
    detail.textContent = `${account.email} · ${account.password}`;
    button.append(role, detail);
    button.addEventListener("click", () => {
      $("#login-email").value = account.email;
      $("#login-password").value = account.password;
      submit.focus();
    });
    item.append(button);
    list.append(item);
  }
  $("#demo-accounts").hidden = false;
}

notice();
demoAccounts();
submit.disabled = false; // enabled only once the handler above is attached
