/* Native disclosures and form fields retain their normal keyboard behavior. */
document.querySelectorAll('[data-business-expectations-form]').forEach((form) => {
  form.querySelectorAll('[data-expectation-mode]').forEach((select) => {
    const update = () => {
      const model = select.closest('.expectation-model');
      if (!model) return;
      model.querySelectorAll('[data-expectation-phase]').forEach((group) => {
        const allowed = select.value === 'upsert' || select.value === group.dataset.expectationPhase;
        group.querySelectorAll('input, select').forEach((input) => { input.disabled = !allowed; });
      });
    };
    select.addEventListener('change', update);
    update();
  });
});
const expectationError = document.querySelector('[data-expectation-error]');
if (expectationError) expectationError.focus();
else if (location.hash.startsWith('#expect-')) {
  const model = document.getElementById(decodeURIComponent(location.hash.slice(1)));
  if (model) model.focus();
}
