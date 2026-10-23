/**
 * The Review page, driven end to end (ADR 0049).
 *
 * Each test starts from a fresh queue of the suite's own (`seedReviewTasks`, into
 * `LABELS_DIR`), because the page always shows the next unanswered task and every test here
 * answers one. The witness is the server: after a click, the ledger's answer as
 * `GET /api/labels/tasks/{id}` reports it — a page that looked answered and recorded
 * nothing would pass any assertion made only against the page.
 */
import { expect, test as base, type Page } from "@playwright/test";
import { seedReviewTasks } from "./seed";

const test = base.extend<{ review: Page }>({
  review: async ({ page }, use) => {
    seedReviewTasks(2);
    await page.goto("/");
    await page.getByRole("button", { name: "Review", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Which would you rather run?" })).toBeVisible();
    await use(page);
  },
});

async function stored(page: Page, id: string) {
  const response = await page.request.get(`/api/labels/tasks/${id}`);
  expect(response.status()).toBe(200);
  return response.json();
}

test("a question is two lines and nothing that would decide it", async ({ review }) => {
  const legend = review.locator(".legend");
  await expect(legend).toContainText("A");
  await expect(legend).toContainText("km");
  await expect(review.locator("canvas.maplibregl-canvas")).toBeVisible();

  // Neither on the page nor on the wire before an answer: the server holds the shares back.
  await expect(review.getByRole("status")).toHaveCount(0);
  await expect(review.getByText("Unpaved", { exact: true })).toHaveCount(0);
  const task = await stored(review, "gesture-01");
  expect(task.shares).toBeNull();
  expect(JSON.stringify(task)).not.toContain("drawn_by");
});

test("a choice with no strength is asked for one, and nothing is recorded", async ({ review }) => {
  await review.getByRole("button", { name: "A", exact: true }).click();

  await expect(review.getByLabel("Not recorded")).toContainText("Say how strongly");
  expect((await stored(review, "gesture-01")).answer).toBeNull();
});

test("a choice is recorded, its numbers appear, and the next question loads", async ({
  review,
}) => {
  await review.getByRole("radio", { name: "clearly" }).check();
  await review.getByRole("button", { name: "B", exact: true }).click();

  await expect(review.getByRole("status")).toContainText("Clearly B");
  await expect(review.getByRole("status")).toContainText("On a path or footway");
  const answered = await stored(review, "gesture-01");
  expect(answered.answer.prefer).toBe("b");
  expect(answered.answer.via).toBe("ui");
  expect(answered.shares.b.path).toBe(1);

  await review.getByRole("button", { name: "Next question" }).click();
  await expect(review.getByRole("status")).toHaveCount(0);
  await expect(review.getByText("1 answered, 1 to go")).toBeVisible();
});

test("no preference is an answer, and a first answer stands", async ({ review }) => {
  await review.getByRole("button", { name: "No preference" }).click();
  await expect(review.getByRole("status")).toContainText("No preference");

  const again = await review.request.post("/api/labels/tasks/gesture-01/answer", {
    data: { prefer: "a", strength: "clear" },
  });
  expect(again.status()).toBe(409);
  expect((await stored(review, "gesture-01")).answer.prefer).toBe("none");
});
