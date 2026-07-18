(function () {
    "use strict";

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const revealItems = document.querySelectorAll("[data-reveal]");
    if (revealItems.length) {
        if (reducedMotion || !("IntersectionObserver" in window)) {
            revealItems.forEach((item) => item.classList.add("is-revealed"));
        } else {
            const revealObserver = new IntersectionObserver((entries, observer) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    entry.target.classList.add("is-revealed");
                    observer.unobserve(entry.target);
                });
            }, { rootMargin: "0px 0px -8%", threshold: 0.12 });
            revealItems.forEach((item) => revealObserver.observe(item));
        }
    }

    const productionStory = document.querySelector("[data-production-story]");
    if (productionStory) {
        const visual = productionStory.querySelector("[data-production-visual]");
        const number = visual?.querySelector("[data-production-number]");
        const label = visual?.querySelector("[data-production-label]");
        const steps = Array.from(productionStory.querySelectorAll("[data-production-step]"));
        const frames = Array.from(productionStory.querySelectorAll("[data-production-frame]"));

        const activateStep = (step) => {
            const index = steps.indexOf(step);
            if (index < 0) return;
            steps.forEach((candidate) => candidate.classList.toggle("is-active", candidate === step));
            frames.forEach((frame) => frame.classList.toggle("is-active", frame.dataset.productionFrame === String(index + 1)));
            if (visual) visual.dataset.activeStep = String(index + 1);
            if (number) number.textContent = step.dataset.stepNumber || String(index + 1).padStart(2, "0");
            if (label) label.textContent = step.dataset.stepLabel || step.querySelector("h3")?.textContent || "";
        };

        if (steps.length) {
            let stepFrame = 0;
            const syncActiveStep = () => {
                stepFrame = 0;
                const focusLine = window.innerHeight * .52;
                const closest = steps.reduce((best, step) => {
                    const bounds = step.getBoundingClientRect();
                    const distance = Math.abs(bounds.top + bounds.height / 2 - focusLine);
                    return !best || distance < best.distance ? { step, distance } : best;
                }, null);
                if (closest) activateStep(closest.step);
            };
            const requestStepSync = () => {
                if (stepFrame) return;
                stepFrame = window.requestAnimationFrame(syncActiveStep);
            };
            window.addEventListener("scroll", requestStepSync, { passive: true });
            window.addEventListener("resize", requestStepSync, { passive: true });
            syncActiveStep();
        }
    }

    const hero = document.querySelector("[data-showcase-hero]");
    if (hero && !reducedMotion) {
        let frame = 0;
        const updateHero = () => {
            frame = 0;
            const progress = Math.min(window.scrollY / Math.max(hero.offsetHeight, 1), 1);
            hero.style.setProperty("--hero-y", `${progress * 24}px`);
        };
        window.addEventListener("scroll", () => {
            if (frame) return;
            frame = window.requestAnimationFrame(updateHero);
        }, { passive: true });
        updateHero();
    }
})();
